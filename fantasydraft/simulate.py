"""Monte Carlo simulation of the remainder of a draft."""
from __future__ import annotations

import random
from dataclasses import dataclass, replace
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


class Simulator:
    def __init__(self, state: DraftState):
        self.state = state
        # Opponents' noise (league setting learn_noise) and, for "unknown" teams,
        # strategy, learned from their picks so far.
        self.learned = {}
        unknown = any(t.strategy == "unknown" for t in state.league.teams)
        if state.picks and (state.league.learn_noise or unknown):
            from .learn import learned_profiles

            self.learned = learned_profiles(state)
        self.league = state.league
        self.slots = self.league.starting_slots
        strategies = ("auto", "adp", "rank", "points")
        players = list(state.players.values())
        self.orders = {s: order_for(s, players) for s in strategies}
        self.keys = {s: [base_key(s, p) for p in order] for s, order in self.orders.items()}
        # "unknown" teams without picks yet are drafted as adp.
        self.orders["unknown"], self.keys["unknown"] = self.orders["adp"], self.keys["adp"]

    def _untaken(self, strategy: str, taken: set[str]) -> Iterator[Player]:
        return (p for p in self.orders[strategy] if p.id not in taken)

    def top_untaken(self, strategy: str, taken: set[str], n: int) -> list[Player]:
        out: list[Player] = []
        for p in self._untaken(strategy, taken):
            out.append(p)
            if len(out) == n:
                break
        return out

    def profile(self, team: int):
        """The model used for an opponent: auto while on autodraft, else saved
        strategy with learned noise."""
        if team in self.state.auto_teams:
            return self.state.profile(team)
        profile = self.learned.get(team, self.state.profile(team))
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
        for i, p in enumerate(self._untaken("points", taken)):
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

    def run(self, rng: random.Random, my_next: Player | None = None, i_pick: bool = True) -> SimResult:
        """Simulate to the end of the draft.

        my_next: take this player at my next pick if still there (otherwise
                 fall back to my greedy policy).
        i_pick:  if False, my turns are skipped and nobody is removed, which
                 isolates what opponents alone would take.
        """
        league = self.league
        taken = set(self.state.picks)
        my_roster = self.state.rosters()[league.my_slot]
        taken_at: dict[str, int] = {}
        starts: dict[str, int] = {}
        forced = my_next
        bias = league.position_bias
        targets = league.targets
        counts: list[dict[str, float]] = [{} for _ in range(league.num_teams)]
        if bias:
            for t, roster in enumerate(self.state.rosters()):
                for p in roster:
                    add_to_counts(counts[t], p)

        for pick in range(self.state.current_pick, league.total_picks):
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
            else:
                profile = self.profile(team)
                pool, keys = self._pool(profile.strategy, taken, starts)
                full = full_positions(counts[team], targets) if bias else frozenset()
                choice = choose_pick(profile, pool, rng, pick, keys, full, bias)
                taken_at[choice.id] = pick
                if bias:
                    add_to_counts(counts[team], choice)
            taken.add(choice.id)

        return SimResult(taken_at, my_roster, team_value(my_roster, league))


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
