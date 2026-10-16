"""Learn how each league member drafted in a past season.

Pulls a past season's draft from ESPN (picks, which were autodrafted, team
owners) plus that season's player ranks, ADP and projections, then runs the
same fitting the live dashboard uses: best-fitting strategy, randomness,
autodraft use, reaching, and favourite NBA team. The result can be merged
into the league file's "managers" as starting profiles for this year.

Caveats: ESPN may only keep a season's final (not preseason) projections, and
NBA teams are players' current teams, so treat "favourite team" as a hint.
"""
from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass

from .analyze import analyze
from .espn import EspnClient, parse_player
from .io import apply_log, placeholder_player
from .learn import fit_teams
from .models import DEFAULT_ROSTER_SLOTS, DraftState, League, Player, TeamProfile


@dataclass
class ManagerHistory:
    owner: str
    team: str
    abbrev: str
    picks: int
    auto_picks: int
    best: str | None  # best-fitting strategy (e.g. "rank", "points@consensus")
    best_p: float
    fit: dict[str, float]
    noise: float | None
    top_rank_rate: float
    reach_vs_adp: float
    favorite_team: str  # NBA team they drafted unusually often ("" if none)
    favorite_count: int

    def profile(self) -> dict:
        """Suggested "managers" entry."""
        out: dict = {"match": [x for x in (self.team, self.abbrev) if x]}
        total = self.picks + self.auto_picks
        if total and self.auto_picks / total >= 0.6:
            out["strategy"] = "auto"
        elif self.best and self.best_p >= 0.5 and self.picks >= 5:
            strategy, _, view = self.best.partition("@")
            out["strategy"] = strategy
            if view:
                out["view"] = view
        if self.noise is not None:
            out["noise"] = round(self.noise, 3)
        if self.favorite_team:
            out["favorite_team"] = self.favorite_team
        return out


def draft_state(league_data: dict, players_raw: list[dict], season: int, me: str = ""):
    """That season's draft as a DraftState, plus (owners, names, abbrevs, order, found_me)."""
    names = EspnClient.team_names(league_data)
    owners = EspnClient.team_owners(league_data)
    abbrevs = {t["id"]: t.get("abbrev", "") for t in league_data.get("teams", [])}
    order = EspnClient.pick_order(league_data)
    picks = EspnClient.picks(league_data)
    if not picks:
        raise ValueError("no draft picks found for that season")

    players: dict[str, Player] = {}
    for raw in players_raw:
        p = parse_player(raw, season)
        if p:
            players[p.id] = p
    for lp in picks:
        players.setdefault(lp.player_id, placeholder_player(lp.player_id))

    me_key = me.lower().strip()
    my_slot = next((i for i, tid in enumerate(order) if me_key and me_key in owners.get(tid, "").lower()), None)
    found_me = my_slot is not None
    if not found_me:
        print(f"note: {me!r} not found among the owners; fitting every team", file=sys.stderr)
        my_slot = 0
    slots = EspnClient.roster_slots(league_data) or dict(DEFAULT_ROSTER_SLOTS)
    league = League([TeamProfile(names.get(tid, str(tid)), "unknown", 0.2, manager=owners.get(tid, ""))
                     for tid in order], my_slot, roster_slots=slots)
    slot_of = {tid: i for i, tid in enumerate(order)}

    entries: list[tuple] = []
    on_auto: set[int] = set()
    for lp in picks:
        slot = slot_of.get(lp.team_id)
        if slot is not None and lp.auto != (slot in on_auto):
            entries.append(("auto", slot, lp.auto))
            (on_auto.add if lp.auto else on_auto.discard)(slot)
        entries.append(("pick", lp.player_id))
    state = DraftState(league, players)
    apply_log(state, entries[: len(entries)])
    state.picks = state.picks[: league.total_picks]
    return state, (owners, names, abbrevs, order, found_me)


def last_season_points(players_raw: list[dict], season: int) -> dict[str, float]:
    """Each player's actual fantasy points the season before `season` (what a
    drafter going by last year's numbers sees)."""
    out = {}
    for raw in players_raw:
        info = raw.get("player", raw)
        for s in info.get("stats", []):
            if s.get("statSourceId") == 0 and s.get("statSplitTypeId") == 0 and s.get("seasonId") == season - 1:
                out[str(info.get("id", raw.get("id")))] = float(s.get("appliedTotal", 0.0))
                break
    return out


def study_history(league_data: dict, players_raw: list[dict], season: int, me: str = "") -> str:
    """`study` on a past season's draft, with last season's actual points as an
    extra list, plus the first picks against each list as a data check."""
    from dataclasses import replace

    from .study import study

    state, _ = draft_state(league_data, players_raw, season, me)
    last = last_season_points(players_raw, season)
    # proj_points = last season's actual points (shown as "your proj" -> "last season");
    # espn_points = ESPN's projection for that season.
    state.players = {pid: replace(p, espn_points=p.proj_points, proj_points=last.get(pid, 0.0))
                     for pid, p in state.players.items()}
    lines = [f"Data check, the first picks of {season} (a list is trustworthy if these sit near the top of it):",
             f"  {'pick':>4}  {'player':24}{'ESPN rank':>10}{'ADP':>7}{'ESPN proj':>10}{'last season':>12}"]
    ps = [p for p in state.players.values()]
    by_proj = sorted(ps, key=lambda p: -p.espn_points)
    by_last = sorted(ps, key=lambda p: -p.proj_points)
    for k, pid in enumerate(state.picks[:15]):
        p = state.players[pid]
        lines.append(f"  {k + 1:>4}  {p.name[:23]:24}{p.espn_rank:>10g}{p.adp:>7.1f}"
                     f"{'#' + str(by_proj.index(p) + 1):>10}{'#' + str(by_last.index(p) + 1):>12}")
    return "\n".join(lines) + "\n\n" + study([(str(season), state)]).replace("your proj", "last season")


def analyze_draft(league_data: dict, players_raw: list[dict], season: int, me: str = "") -> list[ManagerHistory]:
    state, (owners, names, abbrevs, order, found_me) = draft_state(league_data, players_raw, season, me)
    league = state.league
    slots = league.roster_slots
    players = state.players

    fits = fit_teams(state)
    if not found_me:  # the "me" slot is never fitted: fit slot 0 with another slot as "me"
        other = League(league.teams, 1, roster_slots=slots)
        refit = fit_teams(DraftState(other, players, state.picks, set(state.auto_teams), set(state.auto_picks)))
        if 0 in refit:
            fits[0] = refit[0]
    tendencies = analyze(state)
    rosters = state.rosters()
    out = []
    for slot, tid in enumerate(order):
        t = tendencies[slot]
        f = fits.get(slot)
        teams = Counter(p.nba_team for p in rosters[slot] if p.nba_team and p.nba_team != "FA")
        fav, count = teams.most_common(1)[0] if teams else ("", 0)
        best = max(f.fit, key=f.fit.get) if f else None
        out.append(ManagerHistory(
            owner=owners.get(tid, ""), team=names.get(tid, ""), abbrev=abbrevs.get(tid, ""),
            picks=t.picks, auto_picks=t.auto_picks, best=best, best_p=f.fit[best] if f else 0.0,
            fit=f.fit if f else {}, noise=f.noise if f else None,
            top_rank_rate=t.top_rank_rate, reach_vs_adp=t.reach_vs_adp,
            # 3+ players from one NBA team in a 13-man roster is rare by chance.
            favorite_team=fav if count >= 3 else "", favorite_count=count,
        ))
    return out


def fetch_history(league_id: int, season: int, espn_s2: str | None, swid: str | None,
                  me: str = "") -> list[ManagerHistory]:
    client = EspnClient(league_id, season, espn_s2, swid)
    data = client.league()
    raw = client.players_raw(limit=500)
    return analyze_draft(data, raw, season, me)


def merge_managers(existing: dict, history: list[ManagerHistory]) -> dict:
    """Add history-based profiles; anything already set by hand is kept."""
    merged = {k: dict(v) for k, v in (existing or {}).items()}
    for h in history:
        if not h.owner:
            continue
        person = next((k for k in merged if k.lower() == h.owner.lower()), h.owner)
        entry = merged.setdefault(person, {})
        for key, value in h.profile().items():
            if key == "match":
                entry["match"] = sorted(set(entry.get("match", [])) | set(value))
            else:
                entry.setdefault(key, value)
    return merged
