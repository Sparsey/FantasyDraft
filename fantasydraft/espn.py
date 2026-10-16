"""Minimal client for ESPN's (unofficial, undocumented) fantasy basketball API.

Endpoint shapes follow what the ESPN web app uses (and what community
libraries such as `espn-api` rely on). ESPN changes these without notice, so
treat this module as best-effort and check the raw JSON (`--dump`) if
something looks off.

Private leagues need the `espn_s2` and `SWID` cookies from a logged-in
browser session (DevTools -> Application -> Cookies -> espn.com).
"""
from __future__ import annotations

import sys
import json
import urllib.request
from dataclasses import dataclass
from typing import Any

from .models import Player

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/{season}"

# defaultPositionId / eligibleSlots ids for basketball.
POSITION_IDS = {1: "PG", 2: "SG", 3: "SF", 4: "PF", 5: "C"}
SLOT_IDS = {
    0: "PG", 1: "SG", 2: "SF", 3: "PF", 4: "C", 5: "G", 6: "F", 7: "SG/SF",
    8: "G/F", 9: "PF/C", 10: "F/C", 11: "UTIL", 12: "BE", 13: "IR",
}
# proTeamId -> NBA team abbreviation (0 = free agent).
NBA_TEAMS = {
    0: "FA", 1: "ATL", 2: "BOS", 3: "NOP", 4: "CHI", 5: "CLE", 6: "DAL", 7: "DEN",
    8: "DET", 9: "GSW", 10: "HOU", 11: "IND", 12: "LAC", 13: "LAL", 14: "MIA",
    15: "MIL", 16: "MIN", 17: "BKN", 18: "NYK", 19: "ORL", 20: "PHI", 21: "PHX",
    22: "POR", 23: "SAC", 24: "SAS", 25: "OKC", 26: "UTA", 27: "WAS", 28: "TOR",
    29: "MEM", 30: "CHA",
}
PROJECTED = 1
# View the ESPN draft room uses to load its player rankings (STANDARD, ROTO).
DRAFT_RANKS_VIEW = "kona_player_info_edit_draft_strategy"  # statSourceId
SEASON_SPLIT = 0  # statSplitTypeId


@dataclass
class LivePick:
    overall: int  # 1-based
    team_id: int
    player_id: str
    auto: bool  # ESPN flagged this pick as made by autodraft


@dataclass
class RosterSpot:
    team_id: int
    team: str
    owner: str
    slot: str  # lineup slot now: PG ... UTIL, BE, IR
    player: "Player"
    injury: str  # ESPN's status: ACTIVE, DAY_TO_DAY, OUT, ...


class EspnClient:
    def __init__(self, league_id: int, season: int, espn_s2: str | None = None, swid: str | None = None):
        self.league_id = league_id
        self.season = season
        self.cookies = {}
        if espn_s2 and swid:
            self.cookies = {"espn_s2": espn_s2, "SWID": swid}

    def _get(self, path: str, views: list[str], fantasy_filter: dict | None = None) -> Any:
        query = "&".join(f"view={v}" for v in views)
        url = f"{BASE.format(season=self.season)}{path}?{query}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        if fantasy_filter is not None:
            req.add_header("X-Fantasy-Filter", json.dumps(fantasy_filter))
        if self.cookies:
            req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in self.cookies.items()))
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)

    @property
    def _league_path(self) -> str:
        return f"/segments/0/leagues/{self.league_id}"

    # --- league --------------------------------------------------------------

    def league(self) -> dict:
        return self._get(self._league_path, ["mSettings", "mTeam", "mDraftDetail"])

    @staticmethod
    def team_names(league: dict) -> dict[int, str]:
        names = {}
        for t in league.get("teams", []):
            name = t.get("name") or f"{t.get('location', '')} {t.get('nickname', '')}".strip()
            names[t["id"]] = name or t.get("abbrev") or f"Team {t['id']}"
        return names

    @staticmethod
    def team_owners(league: dict) -> dict[int, str]:
        """Team id -> owner's name ("First Last", else display name)."""
        members = {}
        for m in league.get("members", []):
            full = f"{m.get('firstName', '')} {m.get('lastName', '')}".strip()
            members[m.get("id")] = full or m.get("displayName", "")
        out = {}
        for t in league.get("teams", []):
            owners = [members.get(o, "") for o in t.get("owners", []) if members.get(o)]
            if owners:
                out[t["id"]] = " & ".join(owners)
        return out

    @staticmethod
    def pick_order(league: dict) -> list[int]:
        return list(league["settings"]["draftSettings"]["pickOrder"])

    @staticmethod
    def roster_slots(league: dict) -> dict[str, int]:
        counts = league["settings"]["rosterSettings"]["lineupSlotCounts"]
        out = {}
        for slot_id, n in counts.items():
            name = SLOT_IDS.get(int(slot_id))
            if n and name and name != "IR":
                out[name] = int(n)
        return out

    @staticmethod
    def scoring(league: dict) -> dict[str, float]:
        """League fantasy scoring as {stat: points}, for stats the sources module knows.

        ESPN basketball stat ids (as used by community ESPN libraries; verify with
        `sources`, whose "median vs ESPN" should be close to 1.00)."""
        ids = {0: "PTS", 1: "BLK", 2: "STL", 3: "AST", 6: "REB", 11: "TO",
               13: "FGM", 14: "FGA", 15: "FTM", 16: "FTA", 17: "3PM"}
        items = league.get("settings", {}).get("scoringSettings", {}).get("scoringItems", [])
        out = {ids[i["statId"]]: float(i.get("points", 0)) for i in items if i.get("statId") in ids}
        # Stats we can't read from projection sources (double-doubles, OREB, ...)
        # are kept under their id, so sources count as missing them and get
        # rescaled to ESPN's level instead of being silently off.
        other = {f"stat{i['statId']}": float(i.get("points", 0)) for i in items
                 if i.get("statId") not in ids and float(i.get("points", 0))}
        if other:
            print(f"note: your league also scores ESPN stat ids {sorted(other)}, which projection "
                  "sources don't have; their numbers will be rescaled to ESPN's level", file=sys.stderr)
        return {**out, **other}

    def league_rosters(self) -> dict:
        """The league with every team's current roster."""
        return self._get(self._league_path, ["mSettings", "mTeam", "mRoster"])

    @staticmethod
    def rosters(league: dict, season: int) -> list[RosterSpot]:
        """Every rostered player: team, lineup slot and the player (with ESPN's
        season projection by your league's scoring)."""
        names = EspnClient.team_names(league)
        owners = EspnClient.team_owners(league)
        out = []
        for t in league.get("teams", []):
            for e in (t.get("roster") or {}).get("entries", []):
                entry = e.get("playerPoolEntry") or {}
                p = parse_player(entry, season)
                info = entry.get("player") or {}
                if p is None:
                    continue
                out.append(RosterSpot(
                    team_id=t["id"], team=names.get(t["id"], str(t["id"])), owner=owners.get(t["id"], ""),
                    slot=SLOT_IDS.get(e.get("lineupSlotId"), str(e.get("lineupSlotId", ""))),
                    player=p, injury=str(info.get("injuryStatus") or "")))
        return out

    @staticmethod
    def picks(league: dict) -> list[LivePick]:
        raw = league.get("draftDetail", {}).get("picks", [])
        return [
            LivePick(
                overall=p["overallPickNumber"],
                team_id=p["teamId"],
                player_id=str(p["playerId"]),
                auto=bool(p.get("autoDraftTypeId")),
            )
            for p in sorted(raw, key=lambda p: p["overallPickNumber"])
            if p.get("playerId", -1) > 0
        ]

    # --- players -------------------------------------------------------------

    def players_raw(self, limit: int = 400, rank_type: str = "STANDARD", defaults: bool = False) -> list[dict]:
        """Players with projections scored by this league's settings, or by
        ESPN's default league if `defaults` (works before a league is activated)."""
        flt = {
            "players": {
                "limit": limit,
                "sortDraftRanks": {"sortPriority": 100, "sortAsc": True, "value": rank_type},
            }
        }
        path = "/segments/0/leaguedefaults/1" if defaults else self._league_path
        players = self._get(path, ["kona_player_info"], flt).get("players", [])
        # The draft room's rankings come from a separate view; copy them over.
        try:
            ranked = self._get(path, [DRAFT_RANKS_VIEW], flt).get("players", [])
        except Exception:
            ranked = []
        merge_draft_ranks(players, ranked)
        return players

    def players(self, limit: int = 400, rank_type: str = "STANDARD", defaults: bool = False) -> list[Player]:
        raw = self.players_raw(limit, rank_type, defaults)
        return [p for r in raw if (p := parse_player(r, self.season, rank_type))]


def parse_player(raw: dict, season: int, rank_type: str = "STANDARD") -> Player | None:
    info = raw.get("player", raw)
    # eligibleSlots uses lineup slot ids (0=PG ... 4=C); defaultPositionId uses 1=PG ... 5=C.
    positions = {SLOT_IDS[s] for s in info.get("eligibleSlots", []) if s in SLOT_IDS and SLOT_IDS[s] in POSITION_IDS.values()}
    if not positions and info.get("defaultPositionId") in POSITION_IDS:
        positions = {POSITION_IDS[info["defaultPositionId"]]}
    if not positions:
        return None

    proj = 0.0
    for s in info.get("stats", []):
        if (s.get("statSourceId") == PROJECTED and s.get("statSplitTypeId") == SEASON_SPLIT
                and s.get("seasonId") == season):
            proj = float(s.get("appliedTotal", 0.0))
            break

    rank = info.get("draftRanksByRankType", {}).get(rank_type, {}).get("rank")
    adp = info.get("ownership", {}).get("averageDraftPosition")
    return Player(
        id=str(info.get("id", raw.get("id"))),
        name=info.get("fullName", "?"),
        positions=frozenset(positions),
        proj_points=proj,
        espn_rank=float(rank) if rank else float("inf"),
        adp=float(adp) if adp else float("inf"),
        nba_team=NBA_TEAMS.get(info.get("proTeamId"), str(info.get("proTeamId", ""))),
    )


def merge_draft_ranks(players: list[dict], ranked: list[dict]) -> int:
    """Copy draftRanksByRankType from draft-room data onto player-info data (by id)."""
    by_id = {}
    for raw in ranked:
        info = raw.get("player", raw)
        if info.get("draftRanksByRankType"):
            by_id[str(info.get("id", raw.get("id")))] = info["draftRanksByRankType"]
    for raw in players:
        info = raw.get("player", raw)
        ranks = by_id.get(str(info.get("id", raw.get("id"))))
        if ranks:
            info["draftRanksByRankType"] = ranks
    return len(by_id)


def draft_ranks_from_capture(events: list[dict]) -> dict[str, dict]:
    """Draft-room rankings found in a userscript capture: id -> {name, STANDARD, ROTO, ...}."""
    out: dict[str, dict] = {}
    for e in events:
        if e.get("kind") not in ("fetch", "xhr") or "draftRanksByRankType" not in str(e.get("data", "")):
            continue
        try:
            body = json.loads(e["data"])
        except (ValueError, TypeError):
            continue
        for raw in body.get("players", []) if isinstance(body, dict) else []:
            info = raw.get("player", raw)
            ranks = info.get("draftRanksByRankType") or {}
            if ranks:
                row = out.setdefault(str(info.get("id", raw.get("id"))), {"name": info.get("fullName", "?")})
                for kind, r in ranks.items():
                    if isinstance(r, dict) and r.get("rank"):
                        row[kind] = r["rank"]
    return out
