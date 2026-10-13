"""Monte Carlo simulation of the remainder of a draft."""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterator

from .drafters import CANDIDATE_POOL, choose_pick, order_for
from .lineup import roster_value
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
        self.league = state.league
        self.slots = self.league.starting_slots
        strategies = {t.strategy for t in self.league.teams} | {"points", "auto"}
        players = list(state.players.values())
        self.orders = {s: order_for(s, players) for s in strategies}

    def _untaken(self, strategy: str, taken: set[str]) -> Iterator[Player]:
        return (p for p in self.orders[strategy] if p.id not in taken)

    def top_untaken(self, strategy: str, taken: set[str], n: int) -> list[Player]:
        out: list[Player] = []
        for p in self._untaken(strategy, taken):
            out.append(p)
            if len(out) == n:
                break
        return out

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
        base = roster_value(roster, self.slots, self.league.bench_weight)
        return max(
            candidates.values(),
            key=lambda p: (roster_value(roster + [p], self.slots, self.league.bench_weight) - base, p.proj_points),
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
        forced = my_next

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
                profile = self.state.profile(team)
                ordered = self.top_untaken(profile.strategy, taken, CANDIDATE_POOL)
                choice = choose_pick(profile, ordered, rng, pick)
                taken_at[choice.id] = pick
            taken.add(choice.id)

        return SimResult(taken_at, my_roster, roster_value(my_roster, self.slots, league.bench_weight))


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
