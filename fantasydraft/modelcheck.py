"""Does learning each team's habits make the pick predictions better?

Replays a finished draft. Before each opponent pick (made by a person, not
autodraft), three predictors say who goes next:
  model     - the dashboard's model, learning from that team's picks so far
  no-learn  - every team drafts around ADP with the default randomness
  espn      - the top available player on ESPN's board (what autodraft takes)
and are scored on how often their top guess was right and the average chance
they gave the player actually taken, early / middle / late in the draft.
"""
from __future__ import annotations

import random
from dataclasses import replace

from .drafters import base_key
from .io import apply_log, is_placeholder
from .models import DraftState, League
from .simulate import Simulator


def _next_pick_odds(state: DraftState, sims: int, seed: int) -> dict[str, float]:
    sim = Simulator(state)
    k = state.current_pick
    rng = random.Random(seed)
    counts: dict[str, int] = {}
    for _ in range(sims):
        pid = sim.run(rng, i_pick=False, until=k + 1).order.get(k)
        if pid:
            counts[pid] = counts.get(pid, 0) + 1
    return {pid: n / sims for pid, n in counts.items()}


def _state_before(league: League, players: dict, entries: list[tuple], k: int) -> DraftState:
    """The draft just before overall pick k (0-based), autodraft switches included."""
    st = DraftState(league, players)
    upto, seen = [], 0
    for e in entries:
        if e[0] == "pick":
            if seen == k:
                break
            seen += 1
        upto.append(e)
    apply_log(st, upto)
    return st


def _flat(lg: League, strategy: str) -> League:
    """Every team as `strategy` with default randomness, no learning."""
    return League([replace(t, strategy=strategy, noise=0.2, view="espn", favorites={}, favorite_team="",
                           team_boost=0.0) for t in lg.teams],
                  lg.my_slot, roster_slots=dict(lg.roster_slots), bench_weight=lg.bench_weight,
                  position_targets=lg.position_targets, balance_penalty=lg.balance_penalty,
                  learn_noise=False, position_bias=lg.position_bias, start_auto=lg.start_auto, autodraft_top=lg.autodraft_top)


def check(state: DraftState, entries: list[tuple], sims: int = 150) -> str:
    lg = state.league
    picks, players = state.picks, state.players
    models = {"model (learning)": lg, "no learning, ADP": _flat(lg, "adp"), "no learning, ESPN rank": _flat(lg, "rank")}
    names = list(models) + ["ESPN board order"]
    # who -> group -> [picks, top-1 hits, top-3 hits, chance given to the actual pick]
    score = {g: {n: [0, 0, 0, 0.0] for n in names} for g in ("people", "people, round 5 on", "autodraft")}
    n_teams = lg.num_teams
    for k, pid in enumerate(picks):
        if lg.team_for_pick(k) == lg.my_slot or is_placeholder(players[pid]):
            continue
        groups = ["autodraft"] if k in state.auto_picks else \
            ["people"] + (["people, round 5 on"] if k >= 4 * n_teams else [])
        taken = set(picks[:k])
        board = sorted((p for p in players.values() if p.id not in taken and not is_placeholder(p)),
                       key=lambda p: base_key("auto", p))[:3]
        board_ids = [p.id for p in board]
        ranked = {"ESPN board order": {pid_: 1.0 if j == 0 else 0.0 for j, pid_ in enumerate(board_ids)}}
        for name, league in models.items():
            if "autodraft" in groups and name != "model (learning)":
                continue
            odds = _next_pick_odds(_state_before(league, players, entries, k), sims, seed=k)
            ranked[name] = dict(sorted(odds.items(), key=lambda kv: -kv[1]))
        for name, odds in ranked.items():
            order = board_ids if name == "ESPN board order" else list(odds)
            for g in groups:
                row = score[g][name]
                row[0] += 1
                row[1] += bool(order) and order[0] == pid
                row[2] += pid in order[:3]
                row[3] += odds.get(pid, 0.0) if name != "ESPN board order" else float(bool(order) and order[0] == pid)

    out = ["How well each predictor called the next pick (higher is better).",
           "top guess: the pick was its most likely player; top 3: among its 3 most likely;",
           "chance: the chance it gave the player actually taken.", ""]
    for g, rows in score.items():
        n = max(rows["model (learning)"][0], rows["ESPN board order"][0])
        if not n:
            continue
        out.append(f"{g} ({n} picks)")
        out.append(f"  {'':24}{'top guess':>10}{'top 3':>8}{'chance':>8}")
        for name in names:
            r = rows[name]
            if not r[0]:
                continue
            chance = f"{r[3] / r[0]:>8.0%}" if name != "ESPN board order" else f"{'':>8}"
            out.append(f"  {name:24}{r[1] / r[0]:>10.0%}{r[2] / r[0]:>8.0%}{chance}")
        out.append("")
    out.append("If 'no learning, ESPN rank' beats 'no learning, ADP' for people, unknown drafters should "
               "start as rank-followers. If autodraft picks miss ESPN's board order, ESPN's autodraft "
               "isn't following the rank list in your sheet.")
    return "\n".join(out)
