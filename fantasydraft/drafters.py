"""Opponent pick models used inside the Monte Carlo simulation."""
from __future__ import annotations

import math
import random
from typing import Iterable, Sequence

from .models import Player, TeamProfile

# Only the best N players by a profile's base key are considered on each pick.
# Noise is relative, so anyone further down effectively never gets picked, and
# this keeps simulations fast.
CANDIDATE_POOL = 30


def _finite(x: float, fallback: float) -> float:
    return x if math.isfinite(x) else fallback


def base_key(strategy: str, p: Player) -> float:
    """Sort key for a strategy; lower is better."""
    if strategy == "unknown":
        strategy = "adp"  # the default guess for a team we know nothing about
    if strategy in ("auto", "rank"):
        return _finite(p.espn_rank, _finite(p.adp, 10_000.0))
    if strategy == "adp":
        return _finite(p.adp, _finite(p.espn_rank, 10_000.0))
    if strategy == "points":
        return -p.proj_points
    raise ValueError(strategy)


def order_for(strategy: str, players: Iterable[Player]) -> list[Player]:
    return sorted(players, key=lambda p: base_key(strategy, p))


def add_to_counts(counts: dict[str, float], p: Player) -> None:
    """Running position counts; a multi-position player counts fractionally."""
    share = 1.0 / len(p.positions)
    for pos in p.positions:
        counts[pos] = counts.get(pos, 0.0) + share


def full_positions(counts: dict[str, float], targets: dict[str, int]) -> frozenset[str]:
    return frozenset(pos for pos, n in targets.items() if counts.get(pos, 0.0) >= n - 1e-9)


def choose_pick(
    profile: TeamProfile,
    ordered: Sequence[Player],
    rng: random.Random,
    pick: int = 0,
    keys: Sequence[float] | None = None,
    full: frozenset[str] = frozenset(),
    bias: float = 0.0,
) -> Player:
    """Pick the player `profile` takes at overall `pick` (0-based).

    `ordered` must be the available players sorted by `base_key(profile.strategy)`
    (at least the first CANDIDATE_POOL of them). `keys` optionally holds those
    base keys, precomputed, to save recomputing them on every simulated pick.
    Players whose positions are all in `full` (the team already has enough
    there) have their slot pushed back by `bias` (0.35 = 35% later).
    """
    if not ordered:
        raise ValueError("no players available")
    if profile.strategy == "auto":
        return ordered[0]

    pool = ordered[:CANDIDATE_POOL]
    best, best_key = pool[0], float("inf")
    for i, p in enumerate(pool):
        if profile.strategy == "points":
            # Use points order as a pseudo draft slot so noise and favorites
            # behave on the same scale as for rank-based strategies.
            key = pick + i + 1.0
        else:
            key = keys[i] if keys is not None else base_key(profile.strategy, p)
        if profile.favorites:
            key -= profile.favorites.get(p.name, 0.0)
        key = max(key, 0.1)
        if bias and full and p.positions <= full:
            key *= 1.0 + bias
        if profile.noise:
            key *= math.exp(rng.gauss(0.0, profile.noise))
        if key < best_key:
            best, best_key = p, key
    return best
