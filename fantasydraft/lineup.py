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

from .models import SLOT_ELIGIBILITY, Player


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
