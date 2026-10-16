"""Extra projection sources, combined into a consensus value per player.

Each source is a table: a Google Sheets tab (pasted or IMPORTHTML-ed), a CSV
file, or a web page address (its biggest stats table is read directly, which
avoids Sheets' size limit, e.g. for Basketball-Reference). Columns are
found by header name, so layouts differ freely:

  player name        PLAYER / Name / Player
  games played       GP / G
  points, rebounds,  PTS, REB/TREB/TRB, AST, STL, BLK, TO/TOV,
  assists, ...       3PM (also "3PTM", "3P", or "3:00 PM" when Sheets has
                     turned the header into a time)
  shooting           FG% / FT% as 0.573 or "0.573(10.5/18.3)" (made/att per
                     game), or separate FGM/FGA/FTM/FTA columns (Basketball-
                     Reference's "FG"/"FT" are makes)

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
import sys
from html.parser import HTMLParser
from dataclasses import dataclass, field, replace
from statistics import mean, pstdev

from .io import _norm, _read_text
from .models import Player

DEFAULT_GAMES = 70.0  # games played when a source gives none

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
    "FG%": ("FGPCT", "FGP"),
    "FT%": ("FTPCT", "FTP"),
    "FGM": ("FGM",), "FGA": ("FGA",), "FTM": ("FTM",), "FTA": ("FTA",),
}
_SHOT = re.compile(r"^\s*([\d.]+)\s*\(\s*([\d.]+)\s*/\s*([\d.]+)\s*\)")


def _key(header: str) -> str | None:
    h = re.sub(r"[^A-Z0-9%]", "", header.upper())
    if h.startswith("3") and "PM" in h:  # "3PM", or Sheets' "3:00 PM"
        return "3PM"
    # Basketball-Reference style: "FG"/"FT" are makes, "FG%"/"FT%" percentages.
    if h in ("FG", "FT"):
        return h + "M"
    if h in ("FG%", "FT%"):
        return h
    if "%" in h:  # 3P%, TOV%, TRB%... are rates, not the counting stats
        return None
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
    # "Name (TEAM - PG)" or FantasyPros' "Name(BKN - SF,PF)DTD": cut at the bracket.
    cell = re.sub(r"\s*\(.*$", "", cell).strip()
    # A status tag glued on: "Tyrese HaliburtonRet", "Joel EmbiidGTD".
    return re.sub(r"(?<=[a-z])(Ret|Inj|INJ|GTD|DTD|Out|OUT|Sus|SUSP|IR|O|Q)$", "", cell)


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
    game_stats: dict[str, float] | None = None  # per-game stats, when GP is known or the source is per game
    pct: dict[str, float] = field(default_factory=dict)  # FG% / FT% as fractions, when given


class _Tables(HTMLParser):
    """Collect every <table> on a page as rows of cell text. Tables hidden in
    HTML comments (as Basketball-Reference does for some) are parsed too."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._stack: list[list[list[str]]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._stack.append([])
        elif tag == "tr" and self._stack:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._stack:
            if self._row:
                self._stack[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._stack:
            self.tables.append(self._stack.pop())

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_comment(self, data):
        if "<table" in data:
            inner = _Tables()
            inner.feed(data)
            self.tables.extend(inner.tables)


def _rows(src: str) -> list[list[str]]:
    """Rows of a source: CSV (sheet tab / file), or the best stats table of a web page."""
    text = _read_text(src)
    if "<table" not in text[:2_000_000].lower():
        return list(csv.reader(_io.StringIO(text)))
    parser = _Tables()
    parser.feed(text)

    def score(table: list[list[str]]) -> int:
        best = max((sum(_key(c) is not None for c in r) for r in table[:5]), default=0)
        return best * 1000 + len(table)

    tables = [t for t in parser.tables if t]
    if not tables:
        raise ValueError(f"{src}: no tables found on the page")
    return max(tables, key=score)


def _is_header(row: list[str]) -> bool:
    return sum(_key(c) is not None for c in row) >= 4 and any(_key(c) == "name" for c in row)


def _merged(top: list[str], below: list[str]) -> list[str]:
    """A header split over two rows (e.g. "Player", "GP" and a group label like
    "Projected per-game stats" on top, the stat names under it): per column,
    the lower row's name where it has one, else the top row's."""
    n = max(len(top), len(below))
    top, below = top + [""] * (n - len(top)), below + [""] * (n - len(below))
    return [b if _key(b) is not None or (b.strip() and _key(a) is None) else a for a, b in zip(top, below)]


def read_source(src: str) -> list[SourceLine]:
    rows = _rows(src)
    header_idx, header = None, None
    for i, r in enumerate(rows):
        if _is_header(r):
            header_idx, header = i, r
            break
        if i + 1 < len(rows) and any(_key(c) == "name" for c in r) and _is_header(_merged(r, rows[i + 1])):
            header_idx, header = i + 1, _merged(r, rows[i + 1])
            break
    if header is None:
        raise ValueError(f"{src}: no header row with a player name and stat columns found")
    cols = {}
    for i, h in enumerate(header):
        k = _key(h)
        if k and k not in cols:
            cols[k] = i
    raw: list[tuple[str, dict[str, float], float | None]] = []
    seen: set[str] = set()
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
        name = clean_name(r[cols["name"]]).rstrip("*")
        # Traded players appear once per team after a combined row: keep the first.
        if "PTS" in stats and _norm(name) not in seen:
            seen.add(_norm(name))
            raw.append((name, stats, stats.pop("GP", None)))
    if not raw:
        raise ValueError(f"{src}: no player rows with points found")
    # Per game if the best scorer averages under ~60 points; then scale by GP.
    per_game = max(s["PTS"] for _, s, _ in raw) < 60
    out = []
    for name, stats, gp in raw:
        pct = {k: (stats[k] / 100 if stats[k] > 1 else stats[k]) for k in ("FG%", "FT%") if stats.get(k)}
        stats = {k: v for k, v in stats.items() if k not in ("FG%", "FT%")}
        if per_game:
            game_stats = stats
            stats = {k: v * (gp if gp else DEFAULT_GAMES) for k, v in stats.items()}
        else:
            game_stats = {k: v / gp for k, v in stats.items()} if gp else None
        out.append(SourceLine(name, gp, stats, per_game, game_stats, pct))
    return out


DEFAULT_FT_RATE = 0.25  # free-throw attempts per field-goal attempt, a typical NBA rate


def estimate_shooting(stats: dict[str, float], pct: dict[str, float], ft_rate: float) -> dict[str, float]:
    """Fill in missing makes/attempts from points, threes and FG%/FT%.

    With f = FG%, t = FT%, r = FTA/FGA:  PTS = 2*FGM + 3PM + FTM
    = 2f*FGA + 3PM + t*r*FGA, so FGA = (PTS - 3PM) / (2f + t*r).
    """
    # Makes and a percentage give the attempts exactly.
    stats = dict(stats)
    for made, att, k in (("FGM", "FGA", "FG%"), ("FTM", "FTA", "FT%")):
        if made in stats and att not in stats and 0 < pct.get(k, 0) <= 1:
            stats[att] = stats[made] / pct[k]
    if all(k in stats for k in ("FGM", "FGA", "FTM", "FTA")) or not ("FG%" in pct and "FT%" in pct):
        return stats
    f, t = pct["FG%"], pct["FT%"]
    if not 0 < f <= 1 or not 0 <= t <= 1 or "PTS" not in stats:
        return stats
    fga = max(stats["PTS"] - stats.get("3PM", 0.0), 0.0) / (2 * f + t * ft_rate)
    out = dict(stats)
    out.setdefault("FGA", fga)
    out.setdefault("FGM", f * fga)
    out.setdefault("FTA", ft_rate * fga)
    out.setdefault("FTM", t * ft_rate * fga)
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
    weight: float = 1.0
    games: str = "source"
    estimated: int = 0  # players whose shooting attempts were estimated from FG%/FT%


def source_config(value) -> dict:
    """A source entry: a link/path, or {"url": ..., "weight": 0.5, "games": "projected"}.

    games "projected": use the source's per-game numbers x the games other
    sources project for the player (for last season's actual stats, where
    injured players' totals would otherwise look terrible).
    """
    if isinstance(value, str):
        return {"url": value, "weight": 1.0, "games": "source"}
    cfg = {"weight": 1.0, "games": "source", **value}
    if cfg["games"] not in ("source", "projected"):
        raise ValueError(f'source games must be "source" or "projected", not {cfg["games"]!r}')
    return cfg


ESPN_WEIGHT = 2.0  # ESPN counts double: the other sources adjust it rather than outvote it


def apply_sources(players: dict[str, Player], sources: dict[str, object],
                  scoring: dict[str, float] | None = None,
                  espn_weight: float = ESPN_WEIGHT) -> list[SourceReport]:
    """Replace each player's proj_points with the consensus of ESPN and the sources.

    Players with a my_proj override keep it. A source missing scoring stats
    (e.g. no attempts) is rescaled to ESPN's level so it doesn't sit lower
    across the board just for lacking a column. The consensus is a weighted
    average (ESPN weight 1, others as configured).
    """
    scoring = scoring or DEFAULT_SCORING
    index: dict[str, str] = {}
    for pid, p in players.items():
        index.setdefault(_norm(p.name), pid)
    configs = {name: source_config(v) for name, v in sources.items()}
    loaded: dict[str, list[tuple[str, SourceLine]]] = {}
    unmatched: dict[str, list[str]] = {}
    for sname, cfg in list(configs.items()):
        try:
            lines = read_source(cfg["url"])
        except Exception as e:  # one bad source shouldn't stop the draft tools
            print(f"warning: source {sname} skipped: {e}", file=sys.stderr)
            del configs[sname]
            continue
        loaded[sname], unmatched[sname] = [], []
        for line in lines:
            pid = index.get(_norm(line.name))
            if pid is None:
                unmatched[sname].append(line.name)
            else:
                loaded[sname].append((pid, line))
    # Games each player is projected to play, from the sources that project games.
    games: dict[str, list[float]] = {}
    for sname, cfg in configs.items():
        if cfg["games"] == "source":
            for pid, line in loaded[sname]:
                if line.gp:
                    games.setdefault(pid, []).append(line.gp)

    # Each player's free-throw rate, from any source with real attempts.
    rates: dict[str, list[float]] = {}
    for sname in configs:
        for pid, line in loaded[sname]:
            if line.stats.get("FGA", 0) > 0 and "FTA" in line.stats:
                rates.setdefault(pid, []).append(line.stats["FTA"] / line.stats["FGA"])
    all_rates = sorted(r for rs in rates.values() for r in rs)
    league_rate = all_rates[len(all_rates) // 2] if all_rates else DEFAULT_FT_RATE

    per_player: dict[str, list[tuple[str, float, float]]] = {
        pid: [("ESPN", p.room_points, espn_weight)] for pid, p in players.items()}
    reports = []
    for sname, cfg in configs.items():
        found: dict[str, float] = {}
        missing: set[str] = set()
        estimated = 0
        for pid, line in loaded[sname]:
            # A per-game table without games played: use the games the other
            # sources project rather than a flat DEFAULT_GAMES for everyone.
            if cfg["games"] == "projected" or (line.per_game and not line.gp):
                if not line.game_stats:
                    continue
                gp = mean(games[pid]) if pid in games else DEFAULT_GAMES
                stats = {k: v * gp for k, v in line.game_stats.items()}
            else:
                stats = line.stats
            if line.pct:
                rate = mean(rates[pid]) if pid in rates else league_rate
                before = len(stats)
                stats = estimate_shooting(stats, line.pct, rate)
                estimated += len(stats) > before
            pts, miss = fantasy_points(stats, scoring)
            missing.update(miss)
            found[pid] = pts
        ratios = sorted(found[pid] / players[pid].room_points for pid in found if players[pid].room_points > 0)
        scale = ratios[len(ratios) // 2] if ratios else 1.0
        calibrate = bool(missing) and scale > 0
        for pid, pts in found.items():
            per_player[pid].append((sname, pts / scale if calibrate else pts, float(cfg["weight"])))
        reports.append(SourceReport(sname, len(loaded[sname]) + len(unmatched[sname]), len(found),
                                    unmatched[sname], sorted(missing), scale, calibrate,
                                    float(cfg["weight"]), cfg["games"], estimated))
    for pid, vals in per_player.items():
        p = players[pid]
        total_w = sum(w for _, _, w in vals)
        consensus = sum(v * w for _, v, w in vals) / total_w if total_w else p.room_points
        totals = [v for _, v, _ in vals]
        players[pid] = replace(p, proj_points=p.proj_points if p.overridden else consensus,
                               sources=tuple((n, round(v, 1)) for n, v, _ in vals),
                               spread=pstdev(totals) if len(totals) > 1 else 0.0)
    return reports


def underrated(players: dict[str, Player], top: int = 200) -> list[tuple[Player, int, int]]:
    """(player, my rank, ESPN rank) for the top players by my value, biggest ESPN underrating first."""
    pool = [p for p in players.values() if math.isfinite(p.proj_points)]
    mine = sorted(pool, key=lambda p: -p.proj_points)[:top]
    espn_order = {p.id: i + 1 for i, p in enumerate(sorted(pool, key=lambda p: -p.room_points))}
    rows = [(p, i + 1, espn_order[p.id]) for i, p in enumerate(mine)]
    return sorted(rows, key=lambda r: r[1] - r[2])
