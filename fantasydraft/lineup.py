"""Roster valuation: best starting lineup + discounted bench.

Choosing which players start is a transversal matroid (a set of players is
"independent" if they can all be matched to distinct starting slots), so the
greedy algorithm — take players by projected points, keep each one if the
starters can still be matched — gives the optimal lineup. The matching is
grown one augmenting path at a time, which keeps this cheap enough to call
thousands of times per simulation.
"""
from __future__ import annotations

from typing import Iterable, Sequence

from .models import SLOT_ELIGIBILITY, League, Player


def assign_lineup(
    roster: Iterable[Player], slots: Sequence[str]
) -> tuple[list[Player | None], list[Player]]:
    """Best lineup as (player in each starting slot, or None if empty; bench)."""
    eligible = [SLOT_ELIGIBILITY[s] for s in slots]
    slot_owner: list[Player | None] = [None] * len(slots)

    def augment(p: Player, seen: set[int]) -> bool:
        for si, positions in enumerate(eligible):
            if si in seen or not (p.positions & positions):
                continue
            seen.add(si)
            owner = slot_owner[si]
            if owner is None or augment(owner, seen):
                slot_owner[si] = p
                return True
        return False

    bench: list[Player] = []
    filled = 0
    for p in sorted(roster, key=lambda p: p.proj_points, reverse=True):
        # A failed augment leaves slot_owner untouched, so the matching stays valid.
        if filled < len(slots) and augment(p, set()):
            filled += 1
        else:
            bench.append(p)
    return slot_owner, bench


def best_lineup(roster: Iterable[Player], slots: Sequence[str]) -> tuple[list[Player], list[Player]]:
    """Split a roster into (starters, bench) maximising starters' projected points."""
    owners, bench = assign_lineup(roster, slots)
    return [p for p in owners if p is not None], bench


def roster_value(roster: Iterable[Player], slots: Sequence[str], bench_weight: float) -> float:
    starters, bench = best_lineup(roster, slots)
    return sum(p.proj_points for p in starters) + bench_weight * sum(p.proj_points for p in bench)


def over_targets(roster: Iterable[Player], league: League) -> list[Player]:
    """Players beyond the league's position targets.

    Multi-position players count toward whichever of their positions has
    room, and the lowest-projected players are the ones left over.
    """
    _, extra = assign_lineup(roster, league.target_slots)
    return extra


def team_value(roster: list[Player], league: League) -> float:
    """Projected value of a roster: best lineup + discounted bench, minus a
    discount on players beyond the position targets (roster balance)."""
    value = roster_value(roster, league.starting_slots, league.bench_weight)
    if league.balance_penalty:
        value -= league.balance_penalty * sum(p.proj_points for p in over_targets(roster, league))
    return value


def target_fill(roster: Iterable[Player], league: League) -> tuple[dict[str, int], list[Player]]:
    """How many players fill each position target (flexible assignment), and who is over."""
    slots = league.target_slots
    owners, extra = assign_lineup(roster, slots)
    filled = {pos: 0 for pos in league.targets}
    for slot, p in zip(slots, owners):
        if p is not None:
            filled[slot] += 1
    return filled, extra
