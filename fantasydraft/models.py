"""Core data types: players, league settings, opponent profiles and draft state."""
from __future__ import annotations

from dataclasses import dataclass, field

POSITIONS = ("PG", "SG", "SF", "PF", "C")

# Which player positions may fill each lineup slot (ESPN basketball defaults).
SLOT_ELIGIBILITY: dict[str, frozenset[str]] = {
    "PG": frozenset({"PG"}),
    "SG": frozenset({"SG"}),
    "SF": frozenset({"SF"}),
    "PF": frozenset({"PF"}),
    "C": frozenset({"C"}),
    "G": frozenset({"PG", "SG"}),
    "F": frozenset({"SF", "PF"}),
    "SG/SF": frozenset({"SG", "SF"}),
    "G/F": frozenset({"PG", "SG", "SF", "PF"}),
    "PF/C": frozenset({"PF", "C"}),
    "F/C": frozenset({"SF", "PF", "C"}),
    "UTIL": frozenset(POSITIONS),
}
BENCH = "BE"

DEFAULT_ROSTER_SLOTS = {
    "PG": 1, "SG": 1, "SF": 1, "PF": 1, "C": 1,
    "G": 1, "F": 1, "UTIL": 3, "BE": 3,
}


@dataclass(frozen=True)
class Player:
    id: str
    name: str
    positions: frozenset[str]
    proj_points: float
    espn_rank: float = float("inf")
    adp: float = float("inf")
    nba_team: str = ""
    # ESPN's own projection (what the draft room shows). proj_points is *my*
    # value: ESPN's, a my_proj override, or a consensus of several sources.
    espn_points: float = float("nan")
    overridden: bool = False  # my_proj set in the sheet
    sources: tuple[tuple[str, float], ...] = ()  # (source name, season points) incl. ESPN
    spread: float = 0.0  # disagreement between sources (std dev of season points)

    @property
    def room_points(self) -> float:
        """Projection opponents see in the ESPN draft room."""
        return self.espn_points if self.espn_points == self.espn_points else self.proj_points

    def __repr__(self) -> str:  # keep debug output short
        return f"Player({self.name!r}, {'/'.join(sorted(self.positions))}, {self.proj_points:.0f})"


@dataclass
class TeamProfile:
    """How an opponent is expected to draft.

    strategy:
      - "auto":   ESPN autodraft: the best available player by `espn_rank`,
                  except that it passes him with chance 1 - auto_top (and the
                  next one with the same chance, and so on). auto_top is learned
                  from the draft's autodraft picks (ESPN mock rooms: about 0.5).
      - "adp":    drafts roughly by average draft position, with noise.
      - "rank":   drafts roughly by ESPN rank, with noise (a human following
                  the ESPN draft board).
      - "points": drafts roughly by projected points, with noise (a "deep"
                  drafter who has done their own homework).
      - "balanced": a knowledgeable drafter building a well-rounded team:
                  ranks players by projected points scaled by how much their
                  roster still needs that position, with (usually low) noise.
      - "feel":   picks by feel among ESPN's top suggestions: chooses from
                  the top `spread` players by ESPN rank, fairly evenly
                  (#1 only mildly favoured), never outside them.
      view decides whose projections "points"/"balanced" drafters use (see below).
      - "unknown": not set; the simulation uses whichever strategy best fits
                  the team's picks so far (adp before their first).
    noise: relative standard deviation applied to the sort key (0.15 = ±15%).
    spread: for "feel", how many of ESPN's top-ranked players they choose among.
    favorites: player name -> how many spots earlier they will take that
               player than the strategy otherwise would.
    """

    name: str
    strategy: str = "unknown"
    noise: float = 0.3  # people in ESPN mocks: about 0.3 (fantasydraft study)
    favorites: dict[str, float] = field(default_factory=dict)
    spread: int = 8
    # Whose projections a "points"/"balanced" drafter believes: ESPN's (what
    # the draft room shows), the outside consensus (does homework), or a mix.
    view: str = "espn"
    favorite_team: str = ""  # NBA team abbreviation they're a fan of, e.g. "BOS"
    team_boost: float = 0.0  # draft spots earlier they take that team's players
    manager: str = ""  # the person (from the league file's "managers")
    auto_top: float = 1.0  # "auto": chance it takes the top of ESPN's list (see above)

    VALID_STRATEGIES = ("unknown", "auto", "adp", "rank", "points", "balanced", "feel")
    VALID_VIEWS = ("espn", "consensus", "mix")

    def __post_init__(self) -> None:
        if self.strategy not in self.VALID_STRATEGIES:
            raise ValueError(
                f"team {self.name!r}: unknown strategy {self.strategy!r}, "
                f"expected one of {self.VALID_STRATEGIES}"
            )
        if self.view not in self.VALID_VIEWS:
            raise ValueError(f"team {self.name!r}: view must be one of {self.VALID_VIEWS}")

    @property
    def order_key(self) -> str:
        """Which ordered list this drafter picks from ("points@consensus", "rank", ...)."""
        if self.strategy in ("points", "balanced") and self.view != "espn":
            return f"points@{self.view}"
        return self.strategy


@dataclass
class League:
    teams: list[TeamProfile]  # in first-round draft order
    my_slot: int  # 0-based index into `teams`
    roster_slots: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_ROSTER_SLOTS))
    bench_weight: float = 0.86
    # Roster balance: how many players of each position you aim for, and how
    # much a player beyond those targets is discounted (0.2 = worth 80%).
    # None = an even split, rounded up (3 each for a 13-player roster).
    position_targets: dict[str, int] | None = None
    balance_penalty: float = 0.05
    # Learn each opponent's randomness from their own picks (fantasydraft.learn).
    learn_noise: bool = True
    # Opponents (not on autodraft) avoid positions they've already filled to
    # the targets: such players' draft slot is pushed back by this fraction.
    position_bias: float = 0.1  # mocks: drafters barely avoid filled positions (fantasydraft study)
    # Teams that start the draft on autodraft (saved strategy "auto" in the
    # league file). It's the same flag as the live autodraft switch.
    start_auto: frozenset[int] = frozenset()
    # Before seeing any autodraft picks: the chance autodraft takes the top of
    # ESPN's list (then learned from its picks). ESPN's autopick in a real
    # league: always (1.0). ESPN mock rooms: about half the time (0.5).
    autodraft_top: float = 1.0
    # Share of games a player misses: a position with only one player who can
    # play it loses that much of his value (nobody covers the slot).
    cover_rate: float = 0.03

    def __post_init__(self) -> None:
        for slot in self.roster_slots:
            if slot != BENCH and slot not in SLOT_ELIGIBILITY:
                raise ValueError(f"unknown roster slot {slot!r}")
        if not 0 <= self.my_slot < len(self.teams):
            raise ValueError("my_slot out of range")
        if self.position_targets is not None:
            unknown = set(self.position_targets) - set(POSITIONS)
            if unknown:
                raise ValueError(f"position_targets: unknown positions {sorted(unknown)} (use {POSITIONS})")

    @property
    def targets(self) -> dict[str, int]:
        if self.position_targets is not None:
            return {pos: int(self.position_targets.get(pos, 0)) for pos in POSITIONS}
        even = -(-self.rounds // len(POSITIONS))  # ceil
        return {pos: even for pos in POSITIONS}

    @property
    def target_slots(self) -> list[str]:
        return [pos for pos, n in self.targets.items() for _ in range(n)]

    @property
    def num_teams(self) -> int:
        return len(self.teams)

    @property
    def rounds(self) -> int:
        return sum(self.roster_slots.values())

    @property
    def total_picks(self) -> int:
        return self.num_teams * self.rounds

    @property
    def starting_slots(self) -> list[str]:
        return [s for s, n in self.roster_slots.items() if s != BENCH for _ in range(n)]

    def team_for_pick(self, pick: int) -> int:
        """Team index on the clock for 0-based overall pick number (snake order)."""
        rnd, idx = divmod(pick, self.num_teams)
        return idx if rnd % 2 == 0 else self.num_teams - 1 - idx

    def picks_for_team(self, team: int, start: int = 0) -> list[int]:
        return [p for p in range(start, self.total_picks) if self.team_for_pick(p) == team]


@dataclass
class DraftState:
    league: League
    players: dict[str, Player]
    picks: list[str] = field(default_factory=list)  # player ids in pick order
    # Teams currently on autodraft (temporary; their saved strategy is kept
    # in League.teams and used again once they're switched back).
    auto_teams: set[int] = field(default_factory=set)
    # Indices into `picks` that were made while the picking team was on auto.
    auto_picks: set[int] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.auto_teams |= set(self.league.start_auto)

    def profile(self, team: int) -> TeamProfile:
        base = self.league.teams[team]
        if team in self.auto_teams:
            return TeamProfile(base.name, "auto")
        return base

    @property
    def current_pick(self) -> int:
        return len(self.picks)

    @property
    def is_complete(self) -> bool:
        return self.current_pick >= self.league.total_picks

    def available(self) -> list[Player]:
        taken = set(self.picks)
        return [p for pid, p in self.players.items() if pid not in taken]

    def rosters(self) -> list[list[Player]]:
        out: list[list[Player]] = [[] for _ in range(self.league.num_teams)]
        for i, pid in enumerate(self.picks):
            out[self.league.team_for_pick(i)].append(self.players[pid])
        return out

    def my_next_picks(self) -> list[int]:
        return self.league.picks_for_team(self.league.my_slot, self.current_pick)
