"""Loading and saving players, league config and picks."""
from __future__ import annotations

import csv
import io
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

from .models import DEFAULT_ROSTER_SLOTS, POSITIONS, DraftState, League, Player, TeamProfile

PLAYER_COLUMNS = ["id", "name", "positions", "nba_team", "proj_points", "espn_rank", "adp"]


_BLANKS = {"", "-", "--", "—", "n/a", "na", "?", "none"}


def _float(value: str | None) -> float:
    # Sheets exports may carry thousands separators ("1,234.5").
    value = (value or "").strip().replace(",", "")
    return float("inf") if value.lower() in _BLANKS else float(value)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"x", "y", "yes", "true", "1"}


def sheet_csv_url(url: str) -> str:
    """Turn a Google Sheets link (edit/view URL) into its CSV export URL.

    The sheet must be shared as "Anyone with the link can view". The tab is
    taken from `gid=` in the link (the first tab if absent). Other URLs are
    returned unchanged.
    """
    if "output=csv" in url or "/export" in url:
        return url
    gid = re.search(r"[#?&]gid=(\d+)", url)
    pub = re.match(r"(https://docs\.google\.com/spreadsheets/d/e/[\w-]+)/", url)
    if pub:  # File > Share > Publish to web link
        return f"{pub.group(1)}/pub?output=csv" + (f"&gid={gid.group(1)}" if gid else "")
    m = re.match(r"https://docs\.google\.com/spreadsheets/d/([\w-]+)", url)
    if not m:
        return url
    return (f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv"
            + (f"&gid={gid.group(1)}" if gid else ""))


def _read_text(source: str | Path) -> str:
    src = str(source)
    if src.startswith(("http://", "https://")):
        # A browser-like agent: some stats sites refuse unknown clients.
        req = urllib.request.Request(sheet_csv_url(src), headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) fantasydraft"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8-sig")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            why = getattr(e, "code", None) or getattr(e, "reason", None) or e
            hint = " Is the sheet shared as 'Anyone with the link can view'?" if "docs.google.com" in src else ""
            raise RuntimeError(f"couldn't download {src} ({why}).{hint}") from None
    return read_text_file(src)


def read_text_file(path: str | Path) -> str:
    """A local text file in UTF-8 (with or without BOM) or UTF-16 (what
    Windows PowerShell 5 writes with `>`)."""
    data = Path(path).read_bytes()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


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
    bad: list[str] = []
    text = _read_text(source)
    reader = csv.DictReader(io.StringIO(text))
    headers = {(h or "").strip().lower() for h in reader.fieldnames or []}
    if text.lstrip().startswith("<") or "name" not in headers:
        raise ValueError(f"{source} doesn't look like a players table (no 'name' column). "
                         "If it's a Google Sheet, share it as 'Anyone with the link can view'.")
    for line, row in enumerate(reader, start=2):
        # (a None key holds extra cells beyond the header, e.g. a stray comma in a note)
        row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if isinstance(k, str) and isinstance(v, str)}
        name = row.get("name", "")
        if not name or _truthy(row.get("ignore")):
            continue
        proj = row.get("my_proj") or row.get("proj_points")
        espn = row.get("proj_points") or proj
        if not proj:
            skipped.append(name)
            continue
        try:
            pid = row.get("id") or name
            if pid in players:
                print(f"warning: {source} line {line}: id {pid} appears twice "
                      f"({players[pid].name} / {name}); keeping the later row", file=sys.stderr)
            players[pid] = Player(
                id=pid,
                name=name,
                positions=parse_positions(row.get("positions", "")),
                proj_points=_float(proj),
                espn_points=_float(espn),
                overridden=bool(row.get("my_proj")),
                espn_rank=_float(row.get("espn_rank")),
                adp=_float(row.get("adp")),
                nba_team=row.get("nba_team", ""),
            )
        except ValueError as e:
            bad.append(f"line {line} ({name}): {e}")
    if bad:
        print(f"warning: left out {len(bad)} rows of {source} that couldn't be read:\n  "
              + "\n  ".join(bad[:10]), file=sys.stderr)
    if not players:
        raise ValueError(f"no players with projections in {source}")
    names: dict[str, list[str]] = {}
    for p in players.values():
        names.setdefault(_norm(p.name), []).append(p.id)
    dups = [ids for ids in names.values() if len(ids) > 1]
    if dups:
        print("warning: same player name on more than one row (ids "
              + "; ".join(", ".join(ids) for ids in dups[:5]) + "). Delete the extra rows.", file=sys.stderr)
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


_PROFILE_FIELDS = ("strategy", "noise", "spread", "favorites", "view", "favorite_team", "team_boost")


def _profile(name: str, fields: dict, manager: str = "") -> TeamProfile:
    return TeamProfile(
        name=name,
        strategy=fields.get("strategy", "unknown"),
        noise=float(fields.get("noise", 0.15)),
        spread=int(fields.get("spread", 8)),
        favorites={k: float(v) for k, v in fields.get("favorites", {}).items()},
        view=fields.get("view", "espn"),
        favorite_team=str(fields.get("favorite_team", "")).upper(),
        team_boost=float(fields.get("team_boost", 6.0 if fields.get("favorite_team") else 0.0)),
        manager=manager,
    )


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def match_manager(managers: dict, *names: str) -> str | None:
    """The manager (person) whose name or "match" list fits any of a team's names
    (team name, owner name, abbreviation)."""
    keys = {_key(n) for n in names if n}
    for person, fields in (managers or {}).items():
        aliases = [person, *fields.get("match", [])]
        if any(_key(a) in keys for a in aliases if a):
            return person
    return None


def apply_manager(team: TeamProfile, managers: dict, person: str) -> TeamProfile:
    """A team's profile filled in from a manager's profile."""
    return _profile(team.name, managers[person], manager=person)


def load_league(path: str | Path) -> League:
    """League file -> League. Each team's profile comes from the team entry, or
    (if the entry sets no strategy) from the matching person in "managers"."""
    cfg = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    managers = cfg.get("managers") or {}
    teams = []
    for t in cfg["teams"]:
        person = match_manager(managers, t["name"], t.get("owner", ""), t.get("abbrev", ""))
        explicit = t.get("strategy", "unknown") != "unknown"
        if person and not explicit:
            # The person's profile wins; the team entry only fills what it leaves out.
            team_fields = {k: v for k, v in t.items() if k in _PROFILE_FIELDS and k != "strategy"}
            teams.append(_profile(t["name"], {**team_fields, **managers[person]}, manager=person))
        else:
            teams.append(_profile(t["name"], t, manager=person or ""))
    my = cfg["my_team"]
    my_slot = next((i for i, t in enumerate(teams) if t.name == my), None) if isinstance(my, str) else my - 1
    if my_slot is None:
        raise ValueError(f"my_team {my!r} not found in teams")
    # "auto" in the file means "starts on autodraft": that's the autodraft
    # switch, and if they switch it off they're modelled as following ESPN's rank.
    start_auto = frozenset(i for i, t in enumerate(teams) if t.strategy == "auto" and i != my_slot)
    teams = [replace(t, strategy="rank") if t.strategy == "auto" else t for t in teams]
    return League(
        teams=teams,
        my_slot=my_slot,
        roster_slots=cfg.get("roster_slots", dict(DEFAULT_ROSTER_SLOTS)),
        bench_weight=float(cfg.get("bench_weight", 0.4)),
        position_targets=cfg.get("position_targets"),
        balance_penalty=float(cfg.get("balance_penalty", 0.2)),
        learn_noise=bool(cfg.get("learn_noise", True)),
        position_bias=float(cfg.get("position_bias", 0.35)),
        start_auto=start_auto,
        cover_rate=float(cfg.get("cover_rate", 0.12)),
    )


def league_managers(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8-sig")).get("managers") or {}


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
    # "P.J." and "PJ" are the same: drop the dots between initials first.
    text = re.sub(r"\b([a-z])\.(?=[a-z]\b)", r"\1", text)
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
    for n, raw in enumerate(p.read_text(encoding="utf-8-sig").splitlines(), start=1):
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
    cfg = json.loads(Path(league_path).read_text(encoding="utf-8-sig"))
    return _near(cfg.get("players", "data/players.csv"), league_path)


def _near(path: str, league_path: str | Path) -> str:
    """A relative path from the league file: from the current folder if it's
    there, else next to the league file (so commands work from any folder)."""
    if path.startswith(("http://", "https://")) or Path(path).is_absolute() or Path(path).exists():
        return path
    beside = Path(league_path).parent / path
    return str(beside) if beside.exists() else path


def league_sources(league_path: str | Path) -> tuple[dict[str, str], dict[str, float] | None]:
    """The league file's extra projection sources ({name: tab link/path}) and scoring."""
    cfg = json.loads(Path(league_path).read_text(encoding="utf-8-sig"))
    return cfg.get("sources") or {}, cfg.get("scoring")


def apply_league_sources(players: dict[str, Player], league_path: str | Path) -> list:
    sources, scoring = league_sources(league_path)
    if not sources:
        return []
    from .sources import apply_sources

    reports = apply_sources(players, sources, scoring)
    for r in reports:
        print(f"source {r.name}: {r.matched}/{r.rows} players matched"
              + (f", shooting estimated for {r.estimated}" if r.estimated else "")
              + (f" (rescaled x{1 / r.scale:.2f}: missing {', '.join(r.missing_stats)})" if r.calibrated else ""),
              file=sys.stderr)
    return reports


def load_state(players_path: str | Path | None, league_path: str | Path, picks_path: str | Path | None,
               players: dict[str, Player] | None = None) -> DraftState:
    """Players (unless already loaded and passed in), league and picks -> DraftState."""
    if players is None:
        players = load_players(players_path or players_source(league_path))
        apply_league_sources(players, league_path)
    league = load_league(league_path)
    state = DraftState(league=league, players=players)
    if picks_path:
        apply_log(state, load_log(picks_path, players, league))
    if len(state.picks) > league.total_picks:
        raise ValueError("more picks than the draft has")
    if len(players) < league.total_picks:
        print(f"warning: only {len(players)} players for a {league.total_picks}-pick draft; "
              "the end of the draft can't be simulated properly", file=sys.stderr)
    return state
