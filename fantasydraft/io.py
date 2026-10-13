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


def player_by_rank(rank: int, players: dict[str, Player]) -> str | None:
    """Player id with this ESPN rank (as listed in the players sheet), if any."""
    matches = [pid for pid, p in players.items() if p.espn_rank == rank]
    if len(matches) > 1:
        raise ValueError(f"ESPN rank {rank} is shared by {[players[m].name for m in matches]}")
    return matches[0] if matches else None


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


# A picks file is a draft log: one pick per line (player id, name or unique
# name prefix), plus optional autodraft switches:
#   @auto <team>    team went on autodraft
#   @back <team>    team is drafting themselves again (also: @manual)
# <team> is a team name (or unique prefix) or 1-based draft position.
# '#' starts a comment.

AUTO_ON = ("@auto",)
AUTO_OFF = ("@back", "@manual")


def resolve_team(token: str, league: League) -> int:
    token = token.strip()
    if token.isdigit():
        slot = int(token) - 1
        if 0 <= slot < league.num_teams:
            return slot
        raise ValueError(f"no draft position {token} (1-{league.num_teams})")
    low = token.lower()
    names = [t.name.lower() for t in league.teams]
    if low in names:
        return names.index(low)
    partial = [i for i, n in enumerate(names) if n.startswith(low)]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise ValueError(f"no team matches {token!r}")
    raise ValueError(f"{token!r} is ambiguous: {[league.teams[i].name for i in partial]}")


UNKNOWN = "unknown:"


def placeholder_player(pid: str) -> Player:
    """Stand-in for a drafted player missing from the players list, so the pick
    still counts (and every later pick stays with the right team)."""
    return Player(pid, f"Unknown player {pid}", frozenset(POSITIONS), 0.0)


def is_placeholder(p: Player) -> bool:
    return p.name == f"Unknown player {p.id}"


def load_log(path: str | Path, players: dict[str, Player], league: League | None = None) -> list[tuple]:
    """Read a picks file into entries: ("pick", player_id) or ("auto", team, on)."""
    p = Path(path)
    if not p.exists():
        return []
    entries: list[tuple] = []
    seen: set[str] = set()
    for n, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            if line.startswith("@"):
                cmd, _, team = line.partition(" ")
                cmd = cmd.lower()
                if cmd not in AUTO_ON + AUTO_OFF:
                    raise ValueError(f"unknown command {cmd!r} (use @auto / @back)")
                if league is not None:
                    entries.append(("auto", resolve_team(team, league), cmd in AUTO_ON))
                continue
            if line.lower().startswith(UNKNOWN):
                pid = line[len(UNKNOWN):].strip()
                players.setdefault(pid, placeholder_player(pid))
            else:
                pid = resolve_player(line, players)
        except ValueError as e:
            raise ValueError(f"{path} line {n}: {e}") from None
        if pid in seen:
            raise ValueError(f"{path} line {n}: {players[pid].name} picked twice")
        seen.add(pid)
        entries.append(("pick", pid))
    return entries


def save_log(entries: list[tuple], players: dict[str, Player], league: League, path: str | Path) -> None:
    lines = []
    for e in entries:
        if e[0] == "pick":
            p = players[e[1]]
            lines.append(f"{UNKNOWN}{p.id}  # not in players list" if is_placeholder(p) else f"{p.id}  # {p.name}")
        else:
            lines.append(f"{'@auto' if e[2] else '@back'} {league.teams[e[1]].name}")
    Path(path).write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def apply_log(state: DraftState, entries: list[tuple]) -> None:
    """Replay log entries onto a state (picks, who is on auto, which picks were auto)."""
    for e in entries:
        if e[0] == "pick":
            team = state.league.team_for_pick(len(state.picks))
            if team in state.auto_teams:
                state.auto_picks.add(len(state.picks))
            state.picks.append(e[1])
        elif e[2]:
            state.auto_teams.add(e[1])
        else:
            state.auto_teams.discard(e[1])


def load_picks(path: str | Path, players: dict[str, Player]) -> list[str]:
    """Just the picks from a picks file, in order."""
    return [e[1] for e in load_log(path, players) if e[0] == "pick"]


def save_picks(picks: list[str], players: dict[str, Player], path: str | Path) -> None:
    Path(path).write_text("".join(f"{pid}  # {players[pid].name}\n" for pid in picks), encoding="utf-8")


def players_source(league_path: str | Path) -> str:
    """Where to load players from when --players isn't given: league.json's
    "players" entry (a file path or Google Sheets link), else data/players.csv."""
    cfg = json.loads(Path(league_path).read_text(encoding="utf-8"))
    return cfg.get("players", "data/players.csv")


def load_state(players_path: str | Path | None, league_path: str | Path, picks_path: str | Path | None,
               players: dict[str, Player] | None = None) -> DraftState:
    """Players (unless already loaded and passed in), league and picks -> DraftState."""
    if players is None:
        players = load_players(players_path or players_source(league_path))
    league = load_league(league_path)
    state = DraftState(league=league, players=players)
    if picks_path:
        apply_log(state, load_log(picks_path, players, league))
    if len(state.picks) > league.total_picks:
        raise ValueError("more picks than the draft has")
    return state
