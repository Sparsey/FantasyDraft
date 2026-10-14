"""Pick recommendations for my next selection."""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from statistics import mean

from .lineup import team_value
from .models import POSITIONS, DraftState, Player
from .simulate import Simulator, availability


def availability_sims(sims: int) -> int:
    return max(sims * 3, 200)


MIN_AVAIL = 0.05  # below this chance of lasting to my pick, a player isn't shown as an option


@dataclass
class Recommendation:
    player: Player
    marginal_now: float  # immediate gain in my roster value
    p_avail_now: float  # P(still there at my next pick) — 1.0 if I'm on the clock
    p_avail_after: float  # P(still there at the pick after that) if I pass
    exp_final_value: float  # mean final roster value if I take them (or best fallback if gone)
    values: list[float] = field(default_factory=list)  # final roster value in each simulated draft (same seeds for all)


def candidate_pool(state: DraftState, k: int, exclude: set[str] = frozenset()) -> list[Player]:
    """Top players by immediate roster gain, plus the best player at each position."""
    league = state.league
    roster = state.rosters()[league.my_slot]
    base = team_value(roster, league)
    avail = sorted((p for p in state.available() if p.id not in exclude),
                   key=lambda p: p.proj_points, reverse=True)[: k * 4]
    gains = {p.id: team_value(roster + [p], league) - base for p in avail}
    by_gain = sorted(avail, key=lambda p: (gains[p.id], p.proj_points), reverse=True)

    picked: dict[str, Player] = {p.id: p for p in by_gain[:k]}
    if avail:  # always include the highest-projected players you can get: by your numbers and ESPN's
        picked.setdefault(avail[0].id, avail[0])
        espn_top = max((p for p in state.available() if p.id not in exclude), key=lambda p: p.room_points)
        picked.setdefault(espn_top.id, espn_top)
    for pos in POSITIONS:
        best = next((p for p in by_gain if pos in p.positions), None)
        if best is not None:
            picked.setdefault(best.id, best)
    return list(picked.values())


def recommend(
    state: DraftState,
    candidates: int = 8,
    sims: int = 100,
    seed: int = 0,
    avail: dict[str, list[float]] | None = None,
) -> list[Recommendation]:
    """Rank candidates for my next pick by simulated end-of-draft roster value.

    The plan evaluated for each candidate is "take X at my next pick if X is
    still there, otherwise my default pick", so a great player I will almost
    never get scores like the fallback.

    Every candidate is evaluated on the same random seeds (common random
    numbers), so differences between them reflect the pick, not luck.
    """
    if not state.my_next_picks():
        return []
    league = state.league
    roster = state.rosters()[league.my_slot]
    base = team_value(roster, league)
    my_picks = state.my_next_picks()
    on_clock = my_picks[0] == state.current_pick

    # Availability is cheap next to candidate evaluation; use enough runs that
    # "<1%" means something.
    if avail is None:
        avail = availability(state, sims=availability_sims(sims), seed=seed + 1)
    # Players who almost never last to my next pick aren't real options.
    gone = set() if on_clock else {pid for pid, pr in avail.items() if pr and pr[0] < MIN_AVAIL}
    sim = Simulator(state)
    out: list[Recommendation] = []
    for p in candidate_pool(state, candidates, exclude=gone):
        values = [sim.run(random.Random(seed * 1_000_003 + i), my_next=p).my_value for i in range(sims)]
        probs = avail.get(p.id, [])
        out.append(
            Recommendation(
                player=p,
                marginal_now=team_value(roster + [p], league) - base,
                p_avail_now=1.0 if on_clock else (probs[0] if probs else 0.0),
                p_avail_after=probs[1] if len(probs) > 1 else 0.0,
                exp_final_value=mean(values),
                values=values,
            )
        )
    out.sort(key=lambda r: r.exp_final_value, reverse=True)
    return out


@dataclass
class VsBaseline:
    """A candidate compared with taking ESPN's top-projected player (the SAFE pick), draft by draft."""
    diff: float  # mean final-roster difference (+ = better than the baseline)
    p_better: float  # share of simulated drafts where it ends better
    avg_up: float  # average gain in those drafts
    p_worse: float
    avg_down: float  # average loss (negative) in drafts where it ends worse


def versus_highest_projection(recs: list[Recommendation]) -> tuple[Player | None, dict[str, VsBaseline]]:
    """Compare every candidate with the one ESPN projects highest, using the paired
    simulated drafts (same random seeds), so the split reflects the pick itself."""
    if not recs:
        return None, {}
    # Baseline: ESPN's top projection (what autodraft / ESPN's board would take).
    base = max(recs, key=lambda r: (r.player.room_points, r.player.proj_points))
    out = {}
    for r in recs:
        diffs = [a - b for a, b in zip(r.values, base.values)]
        if not diffs:
            continue
        up = [d for d in diffs if d > 0.5]
        down = [d for d in diffs if d < -0.5]
        out[r.player.id] = VsBaseline(
            diff=mean(diffs), p_better=len(up) / len(diffs), avg_up=mean(up) if up else 0.0,
            p_worse=len(down) / len(diffs), avg_down=mean(down) if down else 0.0)
    return base.player, out


def pick_gamble(recs: list[Recommendation], vs: dict[str, VsBaseline], baseline: Player | None,
                min_better: float = 0.4, min_diff: float = -60.0) -> Player | None:
    """The best alternative to the SAFE pick (ESPN's top projection) that is a real coin flip
    in your favour: taking it ends with a better final roster in at least
    `min_better` of the simulated drafts, without costing more than `min_diff`
    on average. None if nothing qualifies."""
    if baseline is None:
        return None
    options = [r for r in recs if r.player.id != baseline.id and r.player.id in vs
               and vs[r.player.id].p_better >= min_better and vs[r.player.id].diff >= min_diff]
    if not options:
        return None
    best = max(options, key=lambda r: (vs[r.player.id].diff, vs[r.player.id].p_better))
    return best.player
