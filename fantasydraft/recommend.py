"""Pick recommendations for my next selection."""
from __future__ import annotations

import random
from dataclasses import dataclass
from statistics import mean

from .lineup import team_value
from .models import POSITIONS, DraftState, Player
from .simulate import Simulator, availability


@dataclass
class Recommendation:
    player: Player
    marginal_now: float  # immediate gain in my roster value
    p_avail_now: float  # P(still there at my next pick) — 1.0 if I'm on the clock
    p_avail_after: float  # P(still there at the pick after that) if I pass
    exp_final_value: float  # mean final roster value if I take them (or best fallback if gone)


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
    avail = availability(state, sims=max(sims * 3, 200), seed=seed + 1)
    # Players who never last to my next pick aren't real options.
    gone = set() if on_clock else {pid for pid, pr in avail.items() if pr and pr[0] == 0}
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
            )
        )
    out.sort(key=lambda r: r.exp_final_value, reverse=True)
    return out
