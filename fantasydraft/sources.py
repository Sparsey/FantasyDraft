"""Extra projection sources, combined into a consensus value per player.

Each source is a table (a Google Sheets tab, CSV file or URL), typically made
by pasting or IMPORTHTML-ing a projections page into its own tab. Columns are
found by header name, so layouts differ freely:

  player name        PLAYER / Name / Player
  games played       GP / G
  points, rebounds,  PTS, REB/TREB/TRB, AST, STL, BLK, TO/TOV,
  assists, ...       3PM (also "3PTM", "3P", or "3:00 PM" when Sheets has
                     turned the header into a time)
  shooting           FG% / FT% as 0.573 or "0.573(10.5/18.3)" (made/att per
                     game), or separate FGM/FGA/FTM/FTA columns

Stats are per game when there's a GP column and the numbers look per-game,
otherwise season totals. Each source's stats are converted to the league's
fantasy scoring, then averaged with ESPN's projection into the consensus
that drives *my* valuation. Opponents still see ESPN's numbers.
"""
from __future__ import annotations

import csv
import io as _io
import math
import re
from dataclasses import dataclass, replace
from statistics import mean, pstdev

from .io import _norm, _read_text
from .models import Player

# ESPN's default head-to-head points scoring.
DEFAULT_SCORING = {"PTS": 1, "3PM": 1, "FGM": 2, "FGA": -1, "FTM": 1, "FTA": -1,
                   "REB": 1, "AST": 2, "STL": 4, "BLK": 4, "TO": -2}

_HEADERS = {
    "name": ("PLAYER", "NAME", "PLAYERNAME"),
    "GP": ("GP", "G", "GAMES"),
    "PTS": ("PTS", "POINTS"),
    "REB": ("REB", "TREB", "TRB", "REBOUNDS"),
    "AST": ("AST", "ASSISTS"),
    "STL": ("STL", "STEALS"),
    "BLK": ("BLK", "BLOCKS"),
    "TO": ("TO", "TOV", "TURNOVERS"),
    "3PM": ("3PM", "3PTM", "3P", "TPM", "3PT", "THREES"),
    "FG%": ("FG", "FGPCT", "FGP"),
    "FT%": ("FT", "FTPCT", "FTP"),
    "FGM": ("FGM",), "FGA": ("FGA",), "FTM": ("FTM",), "FTA": ("FTA",),
}
_SHOT = re.compile(r"^\s*([\d.]+)\s*\(\s*([\d.]+)\s*/\s*([\d.]+)\s*\)")


def _key(header: str) -> str | None:
    h = re.sub(r"[^A-Z0-9%]", "", header.upper())
    if h.startswith("3") and "PM" in h:  # "3PM", or Sheets' "3:00 PM"
        return "3PM"
    h = h.replace("%", "")
    for key, names in _HEADERS.items():
        if h in names:
            return key
    return None


def clean_name(cell: str) -> str:
    """Drop a duplicated abbreviated name ("Nikola JokicN.Jokic" -> "Nikola Jokic")."""
    cell = cell.strip()
    for k in range(len(cell) - 2, 2, -1):
        full = cell[:k]
        if " " not in full:
            break
        first, rest = full.split(" ", 1)
        if cell[k:] == f"{first[0]}.{rest}":
            return full
    return re.sub(r"\s*\(.*?\)\s*$", "", cell)  # "Name (TEAM - PG)" style


def _num(cell: str) -> float | None:
    cell = (cell or "").strip().replace(",", "")
    try:
        return float(cell)
    except ValueError:
        return None


@dataclass
class SourceLine:
    name: str
    gp: float | None
    stats: dict[str, float]  # season totals (or per game x GP)
    per_game: bool


def read_source(src: str) -> list[SourceLine]:
    rows = list(csv.reader(_io.StringIO(_read_text(src))))
    header_idx = next((i for i, r in enumerate(rows)
                       if sum(_key(c) is not None for c in r) >= 4 and any(_key(c) == "name" for c in r)), None)
    if header_idx is None:
        raise ValueError(f"{src}: no header row with a player name and stat columns found")
    header = rows[header_idx]
    cols = {}
    for i, h in enumerate(header):
        k = _key(h)
        if k and k not in cols:
            cols[k] = i
    raw: list[tuple[str, dict[str, float], float | None]] = []
    for r in rows[header_idx + 1:]:
        if len(r) <= cols["name"] or not r[cols["name"]].strip() or _key(r[cols["name"]]) == "name":
            continue  # blank or a repeated header row
        stats: dict[str, float] = {}
        for k, i in cols.items():
            if k == "name" or i >= len(r):
                continue
            cell = r[i]
            m = _SHOT.match(cell)
            if m and k in ("FG%", "FT%"):
                made, att = float(m.group(2)), float(m.group(3))
                stats["FGM" if k == "FG%" else "FTM"] = made
                stats["FGA" if k == "FG%" else "FTA"] = att
            elif (v := _num(cell)) is not None:
                stats[k] = v
        if "PTS" in stats:
            raw.append((clean_name(r[cols["name"]]), stats, stats.pop("GP", None)))
    if not raw:
        raise ValueError(f"{src}: no player rows with points found")
    # Per game if the best scorer averages under ~60 points; then scale by GP.
    per_game = max(s["PTS"] for _, s, _ in raw) < 60
    out = []
    for name, stats, gp in raw:
        if per_game:
            games = gp if gp else 70.0
            stats = {k: v * games for k, v in stats.items() if k not in ("FG%", "FT%")}
        out.append(SourceLine(name, gp, stats, per_game))
    return out


def fantasy_points(stats: dict[str, float], scoring: dict[str, float]) -> tuple[float, list[str]]:
    """Season fantasy points, plus the scoring stats the source didn't have."""
    missing = [k for k, w in scoring.items() if w and k not in stats]
    return sum(w * stats.get(k, 0.0) for k, w in scoring.items()), missing


@dataclass
class SourceReport:
    name: str
    rows: int
    matched: int
    unmatched: list[str]
    missing_stats: list[str]
    scale: float  # median source/ESPN points ratio for matched players
    calibrated: bool


def apply_sources(players: dict[str, Player], sources: dict[str, str],
                  scoring: dict[str, float] | None = None) -> list[SourceReport]:
    """Replace each player's proj_points with the consensus of ESPN and the sources.

    Players with a my_proj override keep it. A source missing scoring stats
    (e.g. no attempts) is rescaled to ESPN's level so it doesn't sit lower
    across the board just for lacking a column.
    """
    scoring = scoring or DEFAULT_SCORING
    index: dict[str, str] = {}
    for pid, p in players.items():
        index.setdefault(_norm(p.name), pid)
    per_player: dict[str, list[tuple[str, float]]] = {pid: [("ESPN", p.room_points)] for pid, p in players.items()}
    reports = []
    for sname, src in sources.items():
        lines = read_source(src)
        found: dict[str, float] = {}
        unmatched = []
        missing: set[str] = set()
        for line in lines:
            pid = index.get(_norm(line.name))
            if pid is None:
                unmatched.append(line.name)
                continue
            pts, miss = fantasy_points(line.stats, scoring)
            missing.update(miss)
            found[pid] = pts
        ratios = sorted(found[pid] / players[pid].room_points for pid in found if players[pid].room_points > 0)
        scale = ratios[len(ratios) // 2] if ratios else 1.0
        calibrate = bool(missing) and scale > 0
        for pid, pts in found.items():
            per_player[pid].append((sname, pts / scale if calibrate else pts))
        reports.append(SourceReport(sname, len(lines), len(found), unmatched, sorted(missing), scale, calibrate))
    for pid, vals in per_player.items():
        p = players[pid]
        totals = [v for _, v in vals]
        consensus = mean(totals)
        players[pid] = replace(p, proj_points=p.proj_points if p.overridden else consensus,
                               sources=tuple((n, round(v, 1)) for n, v in vals),
                               spread=pstdev(totals) if len(totals) > 1 else 0.0)
    return reports


def underrated(players: dict[str, Player], top: int = 200) -> list[tuple[Player, int, int]]:
    """(player, my rank, ESPN rank) for the top players by my value, biggest ESPN underrating first."""
    pool = [p for p in players.values() if math.isfinite(p.proj_points)]
    mine = sorted(pool, key=lambda p: -p.proj_points)[:top]
    espn_order = {p.id: i + 1 for i, p in enumerate(sorted(pool, key=lambda p: -p.room_points))}
    rows = [(p, i + 1, espn_order[p.id]) for i, p in enumerate(mine)]
    return sorted(rows, key=lambda r: r[1] - r[2])
