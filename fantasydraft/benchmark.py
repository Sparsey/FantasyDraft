"""Benchmark: how much does each way of making my picks gain over autodraft?

Runs many simulated drafts against a realistic mixed league and compares
my final roster under different pick policies:

  auto     - take the best available ESPN rank (what ESPN autodraft does)
  points   - take the best available projection
  greedy   - take whoever adds most to my lineup right now (with balance)
  model    - the full recommender, as used on draft day: it does NOT know the
             opponents' true strategies; every opponent starts "unknown" and
             is learned from their picks (autodraft teams are flagged, as the
             draft-room feed would)

Each policy faces the same opponents and random seeds. The score is the
projected value of my final roster: best starting lineup + bench at the
league's bench weight (no balance penalty, so the metric doesn't favour the
model's own preferences). Projections are treated as the truth, so this
measures draft decisions, not projection accuracy.
"""
from __future__ import annotations

import random
import statistics
import time
from dataclasses import dataclass

from .drafters import CANDIDATE_POOL, add_to_counts, choose_pick, full_positions, order_for
from .lineup import roster_value
from .models import DraftState, League, Player, TeamProfile
from .recommend import recommend
from .simulate import Simulator

# A plausible home league: two autodrafters, list followers of varying
# discipline, a knowledgeable balanced drafter and a by-feel drafter.
OPPONENTS = [
    TeamProfile("auto A", "auto"),
    TeamProfile("auto B", "auto"),
    TeamProfile("adp steady", "adp", 0.12),
    TeamProfile("adp loose", "adp", 0.25),
    TeamProfile("rank follower", "rank", 0.10),
    TeamProfile("projections", "points", 0.08),
    TeamProfile("balanced pro", "balanced", 0.08),
    TeamProfile("by feel", "feel", 0.15, spread=8),
    TeamProfile("adp mid", "adp", 0.18),
]
POLICIES = ("auto", "points", "greedy", "model")


@dataclass
class DraftResult:
    policy: str
    seed: int
    slot: int
    value: float
    roster: list[Player]


def _true_league(slot: int, seed: int, base: League) -> League:
    opponents = OPPONENTS[:]
    random.Random(seed).shuffle(opponents)
    teams = opponents[:slot] + [TeamProfile("Me", "adp")] + opponents[slot:]
    return League(teams, slot, roster_slots=dict(base.roster_slots), bench_weight=base.bench_weight,
                  position_targets=base.position_targets, balance_penalty=base.balance_penalty,
                  position_bias=base.position_bias)


def _belief_league(true: League) -> League:
    """What I know on draft day: autodrafters are visible, everyone else is unknown."""
    teams = [t if t.strategy == "auto" or i == true.my_slot else TeamProfile(t.name, "unknown", 0.2)
             for i, t in enumerate(true.teams)]
    return League(teams, true.my_slot, roster_slots=dict(true.roster_slots), bench_weight=true.bench_weight,
                  position_targets=true.position_targets, balance_penalty=true.balance_penalty,
                  position_bias=true.position_bias)


def run_draft(players: dict[str, Player], base: League, policy: str, slot: int, seed: int,
              sims: int, candidates: int) -> DraftResult:
    true = _true_league(slot, seed, base)
    belief = _belief_league(true)
    plist = list(players.values())
    orders = {s: order_for(s, plist) for s in ("adp", "rank", "points")}
    orders.update(auto=orders["rank"], feel=orders["rank"], balanced=orders["points"])
    rng = random.Random(seed * 7919 + 17)
    picks: list[str] = []
    taken: set[str] = set()
    counts: list[dict[str, float]] = [{} for _ in true.teams]
    targets = true.targets
    for pick in range(true.total_picks):
        team = true.team_for_pick(pick)
        if team == slot:
            avail = [p for p in plist if p.id not in taken]
            if policy == "auto":
                choice = min(avail, key=lambda p: (p.espn_rank, p.adp))
            elif policy == "points":
                choice = max(avail, key=lambda p: p.proj_points)
            else:
                state = DraftState(belief, players, list(picks))
                state.auto_teams = {i for i, t in enumerate(true.teams) if t.strategy == "auto"}
                if policy == "greedy":
                    choice = Simulator(state).my_greedy_pick(state.rosters()[slot], taken)
                else:
                    recs = recommend(state, candidates=candidates, sims=sims, seed=seed + pick)
                    choice = recs[0].player
        else:
            prof = true.teams[team]
            pool = [p for p in orders[prof.strategy] if p.id not in taken][:CANDIDATE_POOL]
            full = full_positions(counts[team], targets) if true.position_bias else frozenset()
            choice = choose_pick(prof, pool, rng, pick, None, full, true.position_bias, counts[team], targets)
        picks.append(choice.id)
        taken.add(choice.id)
        add_to_counts(counts[team], choice)
    roster = [players[pid] for i, pid in enumerate(picks) if true.team_for_pick(i) == slot]
    return DraftResult(policy, seed, slot, roster_value(roster, true.starting_slots, true.bench_weight), roster)


def benchmark(players: dict[str, Player], base: League, drafts: int, sims: int, candidates: int,
              policies=POLICIES, log=print) -> dict[str, list[DraftResult]]:
    results: dict[str, list[DraftResult]] = {p: [] for p in policies}
    start = time.time()
    for d in range(drafts):
        slot = d % base.num_teams
        for policy in policies:
            results[policy].append(run_draft(players, base, policy, slot, seed=1000 + d,
                                             sims=sims, candidates=candidates))
        line = "  ".join(f"{p} {results[p][-1].value:,.0f}" for p in policies)
        log(f"draft {d + 1}/{drafts} (pick #{slot + 1}): {line}   [{time.time() - start:.0f}s]")
    return results


def summarize(results: dict[str, list[DraftResult]]) -> str:
    base = results["auto"]
    rows = [f"{'policy':8s} {'avg roster value':>17s} {'gain vs auto':>13s} {'± (95%)':>9s} {'beats auto':>11s}"]
    for policy, res in results.items():
        diffs = [r.value - b.value for r, b in zip(res, base)]
        mean = statistics.mean(r.value for r in res)
        gain = statistics.mean(diffs)
        ci = 1.96 * statistics.stdev(diffs) / len(diffs) ** 0.5 if len(diffs) > 1 and policy != "auto" else 0.0
        wins = sum(d > 0 for d in diffs) / len(diffs)
        rows.append(f"{policy:8s} {mean:17,.0f} {gain:+13,.0f} {ci:9,.0f} {wins:11.0%}")
    return "\n".join(rows)


def rank_vs_projection(players: dict[str, Player], top: int = 150) -> str:
    """How far ESPN's rank order departs from projection order (top players).

    If they're identical, autodraft and drafting by projections make the same
    picks, so the benchmark can't separate them."""
    by_proj = sorted(players.values(), key=lambda p: -p.proj_points)[:top]
    ranked = [p for p in by_proj if p.espn_rank != float("inf")]
    if len(ranked) < 10:
        return "ESPN ranks: missing for most players, so 'auto' falls back to ADP order."
    rank_order = {p.id: i for i, p in enumerate(sorted(ranked, key=lambda p: p.espn_rank))}
    diffs = [abs(rank_order[p.id] - i) for i, p in enumerate(ranked)]
    n = len(ranked)
    rho = 1 - 6 * sum(d * d for d in diffs) / (n * (n * n - 1))
    moved = sum(d >= 5 for d in diffs)
    if max(diffs) == 0:
        return (f"ESPN rank order is IDENTICAL to projection order for the top {n} players, "
                "so 'auto' and 'points' make the same picks. Other projection sources are "
                "what would create a difference.")
    return (f"ESPN rank vs projection order, top {n}: correlation {rho:.2f}, "
            f"{moved} players 5+ spots apart, biggest gap {max(diffs)} spots.")
