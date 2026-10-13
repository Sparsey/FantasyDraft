"""Loading and saving players, league config and picks."""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from .models import DEFAULT_ROSTER_SLOTS, POSITIONS, DraftState, League, Player, TeamProfile

PLAYER_COLUMNS = ["id", "name", "positions", "nba_team", "proj_points", "espn_rank", "adp"]


def _float(value: str | None) -> float:
    value = (value or "").strip()
    return float(value) if value else float("inf")


def parse_positions(text: str) -> frozenset[str]:
    parts = {p.strip().upper() for p in re.split(r"[/,; ]+", text) if p.strip()}
    unknown = parts - set(POSITIONS)
    if unknown:
        raise ValueError(f"unknown positions {sorted(unknown)} in {text!r}")
    if not parts:
        raise ValueError("player has no positions")
    return frozenset(parts)


def load_players(path: str | Path) -> dict[str, Player]:
    """Read a players CSV (columns: see PLAYER_COLUMNS; espn_rank/adp optional)."""
    players: dict[str, Player] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            pid = (row.get("id") or row["name"]).strip()
            players[pid] = Player(
                id=pid,
                name=row["name"].strip(),
                positions=parse_positions(row["positions"]),
                proj_points=float(row["proj_points"]),
                espn_rank=_float(row.get("espn_rank")),
                adp=_float(row.get("adp")),
                nba_team=(row.get("nba_team") or "").strip(),
            )
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


def load_state(players_path: str | Path, league_path: str | Path, picks_path: str | Path | None) -> DraftState:
    players = load_players(players_path)
    league = load_league(league_path)
    picks = load_picks(picks_path, players) if picks_path else []
    if len(picks) > league.total_picks:
        raise ValueError("more picks than the draft has")
    return DraftState(league=league, players=players, picks=picks)
