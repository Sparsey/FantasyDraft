"""Learn each opponent's randomness (and best-fitting strategy) from their picks.

The pick model (drafters.choose_pick) takes the player with the lowest
key x exp(noise x Normal) among the top CANDIDATE_POOL players of a
strategy's list. For every pick a team made itself, we estimate how likely
that exact choice was under each noise level on a grid (by simulation), and
combine those likelihoods across the team's picks with a prior centred on
the noise set in the league file. Teams that keep taking the top of their
list quickly get a small noise; teams that wander get a larger one.

The same likelihoods, summed over the noise grid, say which strategy
(adp / rank / points / balanced / feel) explains a team's picks best.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace

from .drafters import (CANDIDATE_POOL, add_to_counts, balanced_order, base_key, feel_weights,
                       full_positions, order_for)
from .models import DraftState, TeamProfile

NOISE_GRID = (0.02, 0.04, 0.07, 0.10, 0.14, 0.19, 0.25, 0.33, 0.45)
STRATEGIES = ("adp", "rank", "points", "balanced", "feel")
# With extra sources, also test whether a drafter follows the outside consensus.
CONSENSUS_STRATEGIES = ("points@consensus", "balanced@consensus")
# The list each strategy draws its candidates from.
BASE_LIST = {"adp": "adp", "rank": "rank", "points": "points", "balanced": "points", "feel": "rank",
             "points@consensus": "points@consensus", "balanced@consensus": "points@consensus"}


def _strategies(state: DraftState) -> tuple[str, ...]:
    has_consensus = any(abs(p.proj_points - p.room_points) > 1e-6 for p in state.players.values())
    return STRATEGIES + CONSENSUS_STRATEGIES if has_consensus else STRATEGIES
DRAWS = 150  # simulated choices per (pick, strategy, noise)
PRIOR_SPREAD = 0.8  # sd of the prior on log(noise): fairly loose
OUTSIDE = 1e-3  # likelihood of a pick from outside a strategy's top 30
# Recent picks count more, so a drafter who changes approach is noticed: a
# pick's weight halves every RECENT_HALF_LIFE own picks.
RECENT_HALF_LIFE = 6.0
# Unknown teams are simulated as a mix of the strategies that fit; ones below
# this share are dropped.
MIX_FLOOR = 0.05


@dataclass
class TeamFit:
    picks: int  # own (non-autodraft) picks used
    strategy: str  # strategy the simulation uses: the saved one, or best fit if "unknown"
    noise: float  # learned noise for that strategy
    prior_noise: float
    fit: dict[str, float]  # strategy -> posterior probability
    # What the simulation draws from: strategy -> (probability, noise). One
    # entry for a saved strategy; the fitting ones, by fit, for "unknown".
    mix: dict[str, tuple[float, float]] = None


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
    strategies = _strategies(state)
    orders = {b: order_for(b, players) for b in {BASE_LIST[s] for s in strategies}}
    taken: set[str] = set()
    evidence: dict[int, list[dict[str, tuple[float, ...]]]] = {t: [] for t in range(league.num_teams)}
    bias, targets = league.position_bias, league.targets
    counts: list[dict[str, float]] = [{} for _ in range(league.num_teams)]
    for pick, pid in enumerate(state.picks):
        team = league.team_for_pick(pick)
        if pick not in state.auto_picks and team != league.my_slot:
            # Same position-need adjustment the simulation applies.
            full = full_positions(counts[team], targets) if bias else frozenset()
            pools = {}
            for b, order in orders.items():
                pool = []
                for p in order:
                    if p.id not in taken:
                        pool.append(p)
                        if len(pool) == CANDIDATE_POOL:
                            break
                pools[b] = pool
            row = {}
            for s in strategies:
                pool = pools[BASE_LIST[s]]
                if s == "feel":
                    # Even-ish choice among ESPN's top `spread`; noise plays no part.
                    spread = league.teams[team].spread
                    ids = [p.id for p in pool[:spread]]
                    w = feel_weights(len(ids))
                    lik = w[ids.index(pid)] / sum(w) if pid in ids else OUTSIDE
                    row[s] = tuple(lik for _ in NOISE_GRID)
                    continue
                if s.startswith("balanced"):
                    pool = balanced_order(pool, counts[team], targets, s.partition("@")[2] or "espn")
                ids = [p.id for p in pool]
                chosen = ids.index(pid) if pid in ids else len(ids)
                if s.split("@")[0] in ("points", "balanced"):
                    raw = [pick + i + 1.0 for i in range(len(pool))]
                else:
                    raw = [max(base_key(s, p), 0.1) for p in pool]
                if s.startswith("balanced"):  # needs are already in the order
                    keys = tuple(raw)
                else:
                    keys = tuple(k * (1.0 + bias) if full and p.positions <= full else k
                                 for k, p in zip(raw, pool))
                key = (s, pick, chosen, keys)
                if key not in _CACHE:
                    _CACHE[key] = _choice_likelihoods(keys, chosen, seed=pick * 7 + len(s))
                row[s] = _CACHE[key]
            evidence[team].append(row)
        taken.add(pid)
        add_to_counts(counts[team], state.players[pid])
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
        n = len(rows)
        weights = [0.5 ** ((n - 1 - i) / RECENT_HALF_LIFE) for i in range(n)]
        for s in rows[0]:
            logw = [lp + sum(w * math.log(r[s][g]) for w, r in zip(weights, rows))
                    for g, lp in enumerate(log_prior)]
            top = max(logw)
            w = [math.exp(x - top) for x in logw]
            posts[s] = [x / sum(w) for x in w]
            evidence[s] = top + math.log(sum(w))
        top = max(evidence.values())
        z = sum(math.exp(e - top) for e in evidence.values())
        fit = {s: math.exp(e - top) / z for s, e in evidence.items()}
        key = profile.strategy
        if key in ("points", "balanced") and profile.view != "espn":
            key = f"{key}@{profile.view}"
        saved = key if key in fit else max(fit, key=fit.get)

        def noise_for(s: str) -> float:  # posterior mean in log space (noise is a scale)
            return round(math.exp(sum(w * math.log(x) for w, x in zip(posts[s], NOISE_GRID))), 3)
        if key in fit:
            mix = {saved: (1.0, noise_for(saved))}
        else:
            kept = {s: p for s, p in fit.items() if p >= MIX_FLOOR}
            total = sum(kept.values())
            mix = {s: (p / total, noise_for(s)) for s, p in sorted(kept.items(), key=lambda kv: -kv[1])}
        out[team] = TeamFit(len(rows), saved, noise_for(saved), prior_noise, fit, mix)
    return out


def learned_mixes(state: DraftState, fits: dict[int, TeamFit] | None = None
                  ) -> dict[int, list[tuple[float, TeamProfile]]]:
    """Per team, the profiles a simulation draws from, with their probabilities:
    the saved strategy with learned noise, or for "unknown" teams every strategy
    that fits their (recent) picks."""
    fits = fit_teams(state) if fits is None else fits
    out = {}
    for team, f in fits.items():
        profile = state.league.teams[team]
        if profile.strategy == "auto":
            continue
        options = []
        for s, (p, noise) in f.mix.items():
            strategy, _, view = s.partition("@")
            options.append((p, replace(profile, strategy=strategy, view=view or profile.view,
                                       noise=noise if state.league.learn_noise else profile.noise)))
        out[team] = options
    return out


def learned_profiles(state: DraftState) -> dict[int, TeamProfile]:
    """Team profiles with noise replaced by the learned value (non-auto teams);
    "unknown" teams also get their best-fitting strategy."""
    fits = fit_teams(state)
    out = {}
    for team, f in fits.items():
        profile = state.league.teams[team]
        if profile.strategy != "auto":
            noise = f.noise if state.league.learn_noise else profile.noise
            strategy, _, view = f.strategy.partition("@")
            out[team] = replace(profile, strategy=strategy, view=view or profile.view, noise=noise)
    return out
