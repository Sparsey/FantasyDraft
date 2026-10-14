"""Monte Carlo simulation of the remainder of a draft."""
from __future__ import annotations

import random
from dataclasses import dataclass, field, replace
from typing import Iterator

from .drafters import CANDIDATE_POOL, add_to_counts, base_key, choose_pick, full_positions, order_for
from .lineup import team_value
from .models import POSITIONS, DraftState, Player

# My simulated picks consider this many top players by projection, plus the
# best available player at each position.
MY_POLICY_TOP_N = 8


@dataclass
class SimResult:
    taken_at: dict[str, int]  # player id -> overall pick at which an opponent took them
    my_roster: list[Player]
    my_value: float
    order: dict[int, str] = field(default_factory=dict)  # overall pick -> player id (all teams)


class Simulator:
    def __init__(self, state: DraftState):
        self.state = state
        # Opponents' noise (league setting learn_noise) and, for "unknown" teams,
        # strategy, learned from their picks so far.
        self.learned = {}  # team -> best single profile
        self.mixes = {}  # team -> [(probability, profile)] drawn from per simulated draft
        unknown = any(t.strategy == "unknown" for t in state.league.teams)
        if state.picks and (state.league.learn_noise or unknown):
            from .learn import learned_mixes

            self.mixes = learned_mixes(state)
            self.learned = {t: max(opts, key=lambda o: o[0])[1] for t, opts in self.mixes.items()}
        self.league = state.league
        self.slots = self.league.starting_slots
        strategies = ("auto", "adp", "rank", "points", "points@consensus", "points@mix")
        players = list(state.players.values())
        self.orders = {s: order_for(s, players) for s in strategies}
        self.keys = {s: [base_key(s, p) for p in order] for s, order in self.orders.items()}
        # Strategies that share a base list: "unknown" (before any picks) is
        # drafted as adp; "feel" uses ESPN rank; "balanced" uses projections.
        for alias, base in (("unknown", "adp"), ("feel", "rank"), ("balanced", "points")):
            self.orders[alias], self.keys[alias] = self.orders[base], self.keys[base]

    def _untaken(self, strategy: str, taken: set[str]) -> Iterator[Player]:
        return (p for p in self.orders[strategy] if p.id not in taken)

    def top_untaken(self, strategy: str, taken: set[str], n: int) -> list[Player]:
        out: list[Player] = []
        for p in self._untaken(strategy, taken):
            out.append(p)
            if len(out) == n:
                break
        return out

    def draw_profiles(self, rng: random.Random) -> dict:
        """For one simulated draft, pick each unsure team's strategy from its mix."""
        out = {}
        for team, options in self.mixes.items():
            if len(options) > 1:
                r, acc = rng.random(), 0.0
                for p, profile in options:
                    acc += p
                    if r < acc:
                        break
                out[team] = profile
        return out

    def profile(self, team: int, drawn: dict | None = None):
        """The model used for an opponent: auto while on autodraft, else saved
        strategy with learned noise (or this draft's draw for unsure teams)."""
        if team in self.state.auto_teams:
            return self.state.profile(team)
        profile = (drawn or {}).get(team) or self.learned.get(team, self.state.profile(team))
        if profile.strategy == "unknown":  # no picks to learn from yet
            profile = replace(profile, strategy="adp")
        return profile

    def _pool(self, strategy: str, taken: set[str], starts: dict[str, int]) -> tuple[list[Player], list[float]]:
        """First CANDIDATE_POOL untaken players (and their keys) for a strategy.

        `starts` remembers, per simulation run, where the untaken players
        begin, so already-drafted players at the top aren't rescanned.
        """
        order, keys = self.orders[strategy], self.keys[strategy]
        i = starts.get(strategy, 0)
        while i < len(order) and order[i].id in taken:
            i += 1
        starts[strategy] = i
        pool: list[Player] = []
        pool_keys: list[float] = []
        while i < len(order) and len(pool) < CANDIDATE_POOL:
            p = order[i]
            if p.id not in taken:
                pool.append(p)
                pool_keys.append(keys[i])
            i += 1
        return pool, pool_keys

    def my_greedy_pick(self, roster: list[Player], taken: set[str]) -> Player:
        """Take the player who most increases my roster value right now."""
        candidates: dict[str, Player] = {}
        need = set(POSITIONS)
        # Shortlist by my own (consensus) numbers: that's what my roster is valued on.
        for i, p in enumerate(self._untaken("points@consensus", taken)):
            if i < MY_POLICY_TOP_N:
                candidates[p.id] = p
            elif p.positions & need:
                candidates[p.id] = p
            need -= p.positions
            if i >= MY_POLICY_TOP_N and not need:
                break
        base = team_value(roster, self.league)
        return max(
            candidates.values(),
            key=lambda p: (team_value(roster + [p], self.league) - base, p.proj_points),
        )

    def run(self, rng: random.Random, my_next: Player | None = None, i_pick: bool = True,
            until: int | None = None) -> SimResult:
        """Simulate to the end of the draft.

        my_next: take this player at my next pick if still there (otherwise
                 fall back to my greedy policy).
        i_pick:  if False, my turns are skipped and nobody is removed, which
                 isolates what opponents alone would take.
        until:   stop before this overall pick (for short-range predictions).
        """
        league = self.league
        taken = set(self.state.picks)
        my_roster = self.state.rosters()[league.my_slot]
        taken_at: dict[str, int] = {}
        order: dict[int, str] = {}
        starts: dict[str, int] = {}
        forced = my_next
        bias = league.position_bias
        targets = league.targets
        drawn = self.draw_profiles(rng)
        counts: list[dict[str, float]] = [{} for _ in range(league.num_teams)]
        for t, roster in enumerate(self.state.rosters()):
            for p in roster:
                add_to_counts(counts[t], p)

        for pick in range(self.state.current_pick, min(until or league.total_picks, league.total_picks)):
            if len(taken) >= len(self.state.players):
                break  # player list shorter than the draft: nobody left to take
            team = league.team_for_pick(pick)
            if team == league.my_slot:
                if not i_pick:
                    continue
                if forced is not None and forced.id not in taken:
                    choice = forced
                else:
                    choice = self.my_greedy_pick(my_roster, taken)
                forced = None
                my_roster.append(choice)
                order[pick] = choice.id
            else:
                profile = self.profile(team, drawn)
                pool, keys = self._pool(profile.order_key, taken, starts)
                if not pool:
                    break
                full = full_positions(counts[team], targets) if bias else frozenset()
                choice = choose_pick(profile, pool, rng, pick, keys, full, bias, counts[team], targets)
                taken_at[choice.id] = pick
                order[pick] = choice.id
                add_to_counts(counts[team], choice)
            taken.add(choice.id)

        return SimResult(taken_at, my_roster, team_value(my_roster, league), order)


def availability(state: DraftState, sims: int = 200, seed: int = 0) -> dict[str, list[float]]:
    """P(each available player is still there at each of my upcoming picks).

    My own picks are skipped in these simulations, so this answers "if I pass
    on X, will X be there at that turn?" (slightly pessimistic for later picks,
    since the players I would have taken stay in the opponents' pool). Returns player id -> [P(avail at my 1st upcoming
    pick), P(... 2nd), ...].
    """
    sim = Simulator(state)
    my_picks = state.my_next_picks()
    counts = {p.id: [0] * len(my_picks) for p in state.available()}
    rng = random.Random(seed)
    for _ in range(sims):
        result = sim.run(rng, i_pick=False)
        for pid, row in counts.items():
            t = result.taken_at.get(pid)
            for k, mp in enumerate(my_picks):
                if t is None or t > mp:
                    row[k] += 1
    return {pid: [c / sims for c in row] for pid, row in counts.items()}


def predict_picks(state: DraftState, sims: int = 150, horizon: int = 15, seed: int = 0,
                  top: int = 3) -> list[dict]:
    """Most likely players at each of the next `horizon` picks.

    Returns [{"pick": overall pick (0-based), "team": slot, "probs": {player id: P}}],
    where probs holds every player seen at that pick (sorted, most likely first);
    callers can trim to the top few.
    """
    sim = Simulator(state)
    start = state.current_pick
    end = min(start + horizon, state.league.total_picks)
    counts: list[dict[str, int]] = [{} for _ in range(start, end)]
    rng = random.Random(seed)
    for _ in range(sims):
        order = sim.run(rng, until=end).order
        for k in range(start, end):
            pid = order.get(k)
            if pid is not None:
                c = counts[k - start]
                c[pid] = c.get(pid, 0) + 1
    out = []
    for k in range(start, end):
        c = counts[k - start]
        probs = dict(sorted(((pid, n / sims) for pid, n in c.items()), key=lambda kv: -kv[1]))
        out.append({"pick": k, "team": state.league.team_for_pick(k), "probs": probs})
    return out
