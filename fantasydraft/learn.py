"""Learn each opponent's randomness (and best-fitting strategy) from their picks.

The pick model (drafters.choose_pick) takes the player with the lowest
key x exp(noise x Normal) among the top CANDIDATE_POOL players of a
strategy's list. For every pick a team made itself, we estimate how likely
that exact choice was under each noise level on a grid (by simulation), and
combine those likelihoods across the team's picks with a prior centred on
the noise set in the league file. Teams that keep taking the top of their
list quickly get a small noise; teams that wander get a larger one.

The same likelihoods, summed over the noise grid, say which strategy
(adp / rank / points) explains a team's picks best.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace

from .drafters import CANDIDATE_POOL, base_key, order_for
from .models import DraftState, TeamProfile

NOISE_GRID = (0.02, 0.04, 0.07, 0.10, 0.14, 0.19, 0.25, 0.33, 0.45)
STRATEGIES = ("adp", "rank", "points")
DRAWS = 150  # simulated choices per (pick, strategy, noise)
PRIOR_SPREAD = 0.8  # sd of the prior on log(noise): fairly loose
OUTSIDE = 1e-3  # likelihood of a pick from outside a strategy's top 30


@dataclass
class TeamFit:
    picks: int  # own (non-autodraft) picks used
    noise: float  # learned noise for the team's saved strategy
    prior_noise: float
    fit: dict[str, float]  # strategy -> posterior probability


# (strategy, pick index, chosen index, pool keys) -> likelihood per grid noise
_CACHE: dict[tuple, tuple[float, ...]] = {}


def _choice_likelihoods(keys: tuple[float, ...], chosen: int, seed: int) -> tuple[float, ...]:
    """P(model picks `chosen` from a pool with these keys), for each grid noise."""
    if chosen >= len(keys):
        return tuple(OUTSIDE for _ in NOISE_GRID)
    out = []
    logs = [math.log(k) for k in keys]
    for noise in NOISE_GRID:
        rng = random.Random(seed)
        hits = 0
        for _ in range(DRAWS):
            best_j, best_v = 0, float("inf")
            for j, lk in enumerate(logs):
                v = lk + noise * rng.gauss(0.0, 1.0)
                if v < best_v:
                    best_j, best_v = j, v
            hits += best_j == chosen
        out.append((hits + 0.5) / (DRAWS + 1))
    return tuple(out)


def _pick_evidence(state: DraftState) -> dict[int, list[dict[str, tuple[float, ...]]]]:
    """Per team: for each own pick, likelihood-by-noise under each strategy."""
    league = state.league
    players = list(state.players.values())
    orders = {s: order_for(s, players) for s in STRATEGIES}
    taken: set[str] = set()
    evidence: dict[int, list[dict[str, tuple[float, ...]]]] = {t: [] for t in range(league.num_teams)}
    for pick, pid in enumerate(state.picks):
        team = league.team_for_pick(pick)
        if pick not in state.auto_picks and team != league.my_slot:
            row = {}
            for s in STRATEGIES:
                pool = []
                for p in orders[s]:
                    if p.id not in taken:
                        pool.append(p)
                        if len(pool) == CANDIDATE_POOL:
                            break
                ids = [p.id for p in pool]
                chosen = ids.index(pid) if pid in ids else len(ids)
                if s == "points":
                    keys = tuple(pick + i + 1.0 for i in range(len(pool)))
                else:
                    keys = tuple(max(base_key(s, p), 0.1) for p in pool)
                key = (s, pick, chosen, keys)
                if key not in _CACHE:
                    _CACHE[key] = _choice_likelihoods(keys, chosen, seed=pick * 7 + len(s))
                row[s] = _CACHE[key]
            evidence[team].append(row)
        taken.add(pid)
    return evidence


def fit_teams(state: DraftState) -> dict[int, TeamFit]:
    """Learned noise and strategy fit for every opponent with own picks."""
    league = state.league
    out: dict[int, TeamFit] = {}
    for team, rows in _pick_evidence(state).items():
        profile = league.teams[team]
        prior_noise = profile.noise if profile.noise > 0 else 0.15
        log_prior = [-((math.log(n) - math.log(prior_noise)) ** 2) / (2 * PRIOR_SPREAD ** 2) for n in NOISE_GRID]
        if not rows:
            continue
        posts: dict[str, list[float]] = {}
        evidence: dict[str, float] = {}
        for s in STRATEGIES:
            logw = [lp + sum(math.log(r[s][g]) for r in rows) for g, lp in enumerate(log_prior)]
            top = max(logw)
            w = [math.exp(x - top) for x in logw]
            posts[s] = [x / sum(w) for x in w]
            evidence[s] = top + math.log(sum(w))
        top = max(evidence.values())
        z = sum(math.exp(e - top) for e in evidence.values())
        fit = {s: math.exp(e - top) / z for s, e in evidence.items()}
        saved = profile.strategy if profile.strategy in STRATEGIES else max(fit, key=fit.get)
        # Posterior mean in log space (noise is a scale parameter).
        noise = math.exp(sum(w * math.log(n) for w, n in zip(posts[saved], NOISE_GRID)))
        out[team] = TeamFit(len(rows), round(noise, 3), prior_noise, fit)
    return out


def learned_profiles(state: DraftState) -> dict[int, TeamProfile]:
    """Team profiles with noise replaced by the learned value (non-auto teams)."""
    fits = fit_teams(state)
    out = {}
    for team, f in fits.items():
        profile = state.league.teams[team]
        if profile.strategy != "auto":
            out[team] = replace(profile, noise=f.noise)
    return out
