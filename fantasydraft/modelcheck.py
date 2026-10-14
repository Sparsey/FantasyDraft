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


def check(state: DraftState, entries: list[tuple], sims: int = 150) -> str:
    lg = state.league
    flat = League([replace(t, strategy="adp", noise=0.2, view="espn", favorites={}, favorite_team="",
                           team_boost=0.0) for t in lg.teams],
                  lg.my_slot, roster_slots=dict(lg.roster_slots), bench_weight=lg.bench_weight,
                  position_targets=lg.position_targets, balance_penalty=lg.balance_penalty,
                  learn_noise=False, position_bias=lg.position_bias, start_auto=lg.start_auto)
    picks, players = state.picks, state.players
    thirds = [{"n": 0, "model": [0, 0.0], "flat": [0, 0.0], "espn": [0, 0.0]} for _ in range(3)]
    total = len(picks)
    for k, pid in enumerate(picks):
        team = lg.team_for_pick(k)
        if team == lg.my_slot or k in state.auto_picks or is_placeholder(players[pid]):
            continue
        part = thirds[min(3 * k // max(total, 1), 2)]
        part["n"] += 1
        for name, league in (("model", lg), ("flat", flat)):
            odds = _next_pick_odds(_state_before(league, players, entries, k), sims, seed=k)
            top = max(odds, key=odds.get) if odds else None
            part[name][0] += top == pid
            part[name][1] += odds.get(pid, 0.0)
        taken = set(picks[:k])
        board = min((p for p in players.values() if p.id not in taken and not is_placeholder(p)),
                    key=lambda p: base_key("auto", p))
        part["espn"][0] += board.id == pid
        part["espn"][1] += 1.0 if board.id == pid else 0.0

    rows = [("early", thirds[0]), ("middle", thirds[1]), ("late", thirds[2])]
    out = ["Opponent picks made by people (autodraft picks left out); higher is better.", "",
           f"{'':8}{'picks':>6}  {'model':>19}  {'no learning':>17}  {'ESPN board':>11}",
           f"{'':8}{'':>6}  {'top guess / chance':>19}  {'top guess / chance':>17}  {'top guess':>11}"]
    tot = {"n": 0, "model": [0, 0.0], "flat": [0, 0.0], "espn": [0, 0.0]}
    for label, part in rows + [("all", None)]:
        if part is None:
            part = tot
        else:
            for key in ("model", "flat", "espn"):
                tot[key][0] += part[key][0]
                tot[key][1] += part[key][1]
            tot["n"] += part["n"]
        n = max(part["n"], 1)
        out.append(f"{label:8}{part['n']:>6}  {part['model'][0] / n:>9.0%} / {part['model'][1] / n:>7.0%}"
                   f"  {part['flat'][0] / n:>7.0%} / {part['flat'][1] / n:>7.0%}  {part['espn'][0] / n:>11.0%}")
    out += ["", "Top guess: how often the most likely player was the one taken. Avg chance: the chance "
                "given to the player actually taken (a fairer test when nobody is very predictable)."]
    return "\n".join(out)
