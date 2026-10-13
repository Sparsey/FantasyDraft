"""Loading and saving players, league config and picks."""
from __future__ import annotations

import csv
import io
import json
import re
import sys
import unicodedata
import urllib.request
from pathlib import Path

from .models import DEFAULT_ROSTER_SLOTS, POSITIONS, DraftState, League, Player, TeamProfile

PLAYER_COLUMNS = ["id", "name", "positions", "nba_team", "proj_points", "espn_rank", "adp"]


def _float(value: str | None) -> float:
    # Sheets exports may carry thousands separators ("1,234.5").
    value = (value or "").strip().replace(",", "")
    return float(value) if value else float("inf")


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"x", "y", "yes", "true", "1"}


def sheet_csv_url(url: str) -> str:
    """Turn a Google Sheets link (edit/view URL) into its CSV export URL.

    The sheet must be shared as "Anyone with the link can view". The tab is
    taken from `gid=` in the link (the first tab if absent). Other URLs are
    returned unchanged.
    """
    m = re.match(r"https://docs\.google\.com/spreadsheets/d/([\w-]+)", url)
    if not m or "/export" in url or "output=csv" in url:
        return url
    gid = re.search(r"[#?&]gid=(\d+)", url)
    return (f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv"
            + (f"&gid={gid.group(1)}" if gid else ""))


def _read_text(source: str | Path) -> str:
    src = str(source)
    if src.startswith(("http://", "https://")):
        req = urllib.request.Request(sheet_csv_url(src), headers={"User-Agent": "fantasydraft"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8-sig")
    return Path(src).read_text(encoding="utf-8-sig")


def parse_positions(text: str) -> frozenset[str]:
    parts = {p.strip().upper() for p in re.split(r"[/,; ]+", text) if p.strip()}
    unknown = parts - set(POSITIONS)
    if unknown:
        raise ValueError(f"unknown positions {sorted(unknown)} in {text!r}")
    if not parts:
        raise ValueError("player has no positions")
    return frozenset(parts)


def load_players(source: str | Path) -> dict[str, Player]:
    """Read players from a CSV file or a Google Sheets / CSV URL.

    Columns: see PLAYER_COLUMNS (espn_rank, adp, nba_team optional), plus your
    own optional columns:
      my_proj  — overrides proj_points when filled in
      ignore   — x / yes / true to leave the player out entirely
    Any other columns (notes, ...) are ignored. Blank rows, and players with
    no projection, are skipped.
    """
    players: dict[str, Player] = {}
    skipped: list[str] = []
    for line, row in enumerate(csv.DictReader(io.StringIO(_read_text(source))), start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        name = row.get("name", "")
        if not name or _truthy(row.get("ignore")):
            continue
        proj = row.get("my_proj") or row.get("proj_points")
        if not proj:
            skipped.append(name)
            continue
        try:
            pid = row.get("id") or name
            players[pid] = Player(
                id=pid,
                name=name,
                positions=parse_positions(row.get("positions", "")),
                proj_points=_float(proj),
                espn_rank=_float(row.get("espn_rank")),
                adp=_float(row.get("adp")),
                nba_team=row.get("nba_team", ""),
            )
        except ValueError as e:
            raise ValueError(f"{source} line {line} ({name}): {e}") from None
    if skipped:
        print(f"note: skipped {len(skipped)} players with no projection "
              f"(e.g. {', '.join(skipped[:3])})", file=sys.stderr)
    return players


def save_players(players: list[Player], path: str | Path) -> None:
    def num(x: float) -> str:
        return "" if x == float("inf") else f"{x:g}"

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(PLAYER_COLUMNS)
        for p in players:
            w.writerow([p.id, p.name, "/".join(sorted(p.positions)), p.nba_team,
                        f"{p.proj_points:.1f}", num(p.espn_rank), num(p.adp)])


def load_league(path: str | Path) -> League:
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    teams = [
        TeamProfile(
            name=t["name"],
            strategy=t.get("strategy", "adp"),
            noise=float(t.get("noise", 0.15)),
            favorites={k: float(v) for k, v in t.get("favorites", {}).items()},
        )
        for t in cfg["teams"]
    ]
    my = cfg["my_team"]
    my_slot = next((i for i, t in enumerate(teams) if t.name == my), None) if isinstance(my, str) else my - 1
    if my_slot is None:
        raise ValueError(f"my_team {my!r} not found in teams")
    return League(
        teams=teams,
        my_slot=my_slot,
        roster_slots=cfg.get("roster_slots", dict(DEFAULT_ROSTER_SLOTS)),
        bench_weight=float(cfg.get("bench_weight", 0.4)),
    )


def resolve_player(token: str, players: dict[str, Player]) -> str:
    """Match a pick entry (id, exact name, or unique name prefix) to a player id."""
    token = token.strip()
    if token in players:
        return token
    low = token.lower()
    exact = [pid for pid, p in players.items() if p.name.lower() == low]
    if len(exact) == 1:
        return exact[0]
    partial = [pid for pid, p in players.items() if p.name.lower().startswith(low)]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise ValueError(f"no player matches {token!r}")
    raise ValueError(f"{token!r} is ambiguous: {[players[p].name for p in partial[:5]]}")


_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    words = re.sub(r"[^a-z0-9]+", " ", text).split()
    return " ".join(w for w in words if w not in _SUFFIXES)


def extract_picks(text: str, players: dict[str, Player]) -> tuple[list[str], list[str]]:
    """Find players, in order, in text copied from a draft results page.

    Matches full names and "F. Last" abbreviations (when unambiguous),
    ignoring case, accents, punctuation and Jr./III suffixes. Several players
    on one line are taken left to right. Returns (player ids, warnings).
    """
    keys: dict[str, set[str]] = {}
    for pid, p in players.items():
        full = _norm(p.name)
        words = full.split()
        for key in {full, f"{words[0][0]} {' '.join(words[1:])}"} if len(words) > 1 else {full}:
            keys.setdefault(key, set()).add(pid)
    unique = {k: next(iter(v)) for k, v in keys.items() if len(v) == 1}

    picks: list[str] = []
    warnings: list[str] = []
    for line in text.splitlines():
        hay = f" {_norm(line)} "
        found: list[tuple[int, int, str]] = []
        for key, pid in unique.items():
            start = hay.find(f" {key} ")
            while start != -1:
                found.append((start, start + len(key) + 1, pid))
                start = hay.find(f" {key} ", start + 1)
        # Longest match wins where matches overlap ("Jalen Williams" vs "J. Williams").
        found.sort(key=lambda m: (m[0], -(m[1] - m[0])))
        end = -1
        for start, stop, pid in found:
            if start < end:
                continue
            end = stop
            if pid in picks:
                warnings.append(f"{players[pid].name} appears again (ignored): {line.strip()[:60]}")
            else:
                picks.append(pid)
    return picks, warnings


def load_picks(path: str | Path, players: dict[str, Player]) -> list[str]:
    """Picks file: one player (id or name) per line, in draft order. '#' comments allowed."""
    p = Path(path)
    if not p.exists():
        return []
    picks: list[str] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            pid = resolve_player(line, players)
            if pid in picks:
                raise ValueError(f"{players[pid].name} picked twice")
            picks.append(pid)
    return picks


def save_picks(picks: list[str], players: dict[str, Player], path: str | Path) -> None:
    Path(path).write_text("".join(f"{pid}  # {players[pid].name}\n" for pid in picks), encoding="utf-8")


def players_source(league_path: str | Path) -> str:
    """Where to load players from when --players isn't given: league.json's
    "players" entry (a file path or Google Sheets link), else data/players.csv."""
    cfg = json.loads(Path(league_path).read_text(encoding="utf-8"))
    return cfg.get("players", "data/players.csv")


def load_state(players_path: str | Path | None, league_path: str | Path, picks_path: str | Path | None) -> DraftState:
    players = load_players(players_path or players_source(league_path))
    league = load_league(league_path)
    picks = load_picks(picks_path, players) if picks_path else []
    if len(picks) > league.total_picks:
        raise ValueError("more picks than the draft has")
    return DraftState(league=league, players=players, picks=picks)
