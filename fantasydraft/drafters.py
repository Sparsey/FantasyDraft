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

# Standard normal draws, looked up by a uniform random number: much faster
# than random.gauss in the simulation's innermost loop.
_NORMALS = [random.Random(12345).gauss(0.0, 1.0) for _ in range(8192)]


def _finite(x: float, fallback: float) -> float:
    return x if math.isfinite(x) else fallback


def view_points(p: Player, view: str = "espn") -> float:
    """A player's projection as seen by a drafter with this view."""
    if view == "consensus":
        return p.proj_points
    if view == "mix":
        return (p.proj_points + p.room_points) / 2
    return p.room_points


def base_key(strategy: str, p: Player) -> float:
    """Sort key for a strategy (or order key like "points@consensus"); lower is better."""
    strategy, _, view = strategy.partition("@")
    if strategy == "unknown":
        strategy = "adp"  # the default guess for a team we know nothing about
    if strategy in ("auto", "rank", "feel"):
        return _finite(p.espn_rank, _finite(p.adp, 10_000.0))
    if strategy == "adp":
        return _finite(p.adp, _finite(p.espn_rank, 10_000.0))
    if strategy in ("points", "balanced"):
        return -view_points(p, view or "espn")  # by default: ESPN's numbers, as in the room
    raise ValueError(strategy)


# "feel": weight of the i-th best ESPN-ranked player (1-based) is 1/sqrt(i).
def feel_weights(spread: int) -> list[float]:
    return [1.0 / math.sqrt(i) for i in range(1, max(spread, 1) + 1)]


def need_multiplier(p: Player, counts: dict[str, float], targets: dict[str, int]) -> float:
    """How much a "balanced" drafter values a player given their roster:
    full value at a position they still need, less once nearly/fully filled."""
    room = max(targets.get(pos, 0) - counts.get(pos, 0.0) for pos in p.positions)
    if room >= 0.999:
        return 1.0
    if room > 0:
        return 0.85
    return 0.6


def balanced_order(pool: Sequence[Player], counts: dict[str, float], targets: dict[str, int],
                   view: str = "espn") -> list[Player]:
    return sorted(pool, key=lambda p: -view_points(p, view) * need_multiplier(p, counts, targets))


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
    counts: dict[str, float] | None = None,
    targets: dict[str, int] | None = None,
) -> Player:
    """Pick the player `profile` takes at overall `pick` (0-based).

    `ordered` must be the available players sorted by `base_key(profile.strategy)`
    (at least the first CANDIDATE_POOL of them). `keys` optionally holds those
    base keys, precomputed, to save recomputing them on every simulated pick.
    Players whose positions are all in `full` (the team already has enough
    there) have their slot pushed back by `bias` (0.35 = 35% later).
    `counts`/`targets` (the team's position counts and targets) are used by
    the "balanced" strategy.
    """
    if not ordered:
        raise ValueError("no players available")
    strategy = profile.strategy
    if strategy == "auto":
        if not profile.noise:
            return ordered[0]
        bias = 0.0  # autodraft goes by ESPN's list (with the randomness learned from its picks)
    if strategy == "feel":
        top = list(ordered[:max(profile.spread, 1)])
        weights = feel_weights(len(top))
        if profile.favorites:
            weights = [w * (3.0 if p.name in profile.favorites else 1.0) for w, p in zip(weights, top)]
        if profile.team_boost and profile.favorite_team:
            weights = [w * (2.0 if p.nba_team == profile.favorite_team else 1.0) for w, p in zip(weights, top)]
        return rng.choices(top, weights)[0]

    pool = list(ordered[:CANDIDATE_POOL])
    if strategy == "balanced":
        pool = balanced_order(pool, counts or {}, targets or {}, profile.view)
        bias = 0.0  # position needs are already built into the order
    best, best_key = pool[0], float("inf")
    # Keys rise down the list, so once one can't beat the best even with very
    # lucky noise (3.5 sd), nobody further down can: stop there.
    stretch = math.exp(3.5 * profile.noise) if not (profile.favorites or profile.team_boost) else float("inf")
    for i, p in enumerate(pool):
        if strategy in ("points", "balanced"):
            # Use the order as a pseudo draft slot so noise and favorites
            # behave on the same scale as for rank-based strategies.
            key = pick + i + 1.0
        else:
            key = keys[i] if keys is not None else base_key(strategy, p)
        if profile.favorites:
            key -= profile.favorites.get(p.name, 0.0)
        if profile.team_boost and p.nba_team == profile.favorite_team:
            key -= profile.team_boost  # a fan of this team reaches for its players
        key = max(key, 0.1)
        if key > best_key * stretch:
            break
        if bias and full and p.positions <= full:
            key *= 1.0 + bias
        if profile.noise:
            key *= math.exp(_NORMALS[int(rng.random() * 8192)] * profile.noise)
        if key < best_key:
            best, best_key = p, key
    return best
