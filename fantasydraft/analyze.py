"""Infer opponents' draft tendencies from the picks made so far."""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import mean

from .drafters import base_key
from .models import DraftState


@dataclass
class TeamTendency:
    team: str
    picks: int  # picks made by the team itself (not while on autodraft)
    auto_picks: int
    top_rank_rate: float  # share of picks that were the best available ESPN rank
    reach_vs_adp: float  # mean (ADP - pick#): positive = reaches early
    reach_vs_rank: float  # mean (ESPN rank - pick#)
    top_points_rate: float  # share of picks that were the best available projection
    suggested: str


def analyze(state: DraftState) -> list[TeamTendency]:
    league = state.league
    taken: set[str] = set()
    per_team: dict[int, dict[str, list[float]]] = {
        t: {"top_rank": [], "adp": [], "rank": [], "top_pts": []} for t in range(league.num_teams)
    }
    auto_counts = [0] * league.num_teams
    for pick, pid in enumerate(state.picks):
        team = league.team_for_pick(pick)
        p = state.players[pid]
        if pick in state.auto_picks:
            # Autodraft picks say nothing about how the manager drafts.
            auto_counts[team] += 1
            taken.add(pid)
            continue
        avail = [q for q in state.players.values() if q.id not in taken]
        best_rank = min(avail, key=lambda q: base_key("rank", q))
        best_pts = max(avail, key=lambda q: q.room_points)
        stats = per_team[team]
        stats["top_rank"].append(float(best_rank.id == pid))
        stats["top_pts"].append(float(best_pts.id == pid))
        if math.isfinite(p.adp):
            stats["adp"].append(p.adp - (pick + 1))
        if math.isfinite(p.espn_rank):
            stats["rank"].append(p.espn_rank - (pick + 1))
        taken.add(pid)

    out = []
    for t, stats in per_team.items():
        n = len(stats["top_rank"])
        top_rank = mean(stats["top_rank"]) if n else 0.0
        top_pts = mean(stats["top_pts"]) if n else 0.0
        adp = mean(stats["adp"]) if stats["adp"] else 0.0
        rank = mean(stats["rank"]) if stats["rank"] else 0.0
        if n < 3:
            suggested = "?"
        elif top_rank >= 0.9:
            suggested = "auto"
        elif top_pts >= 0.6:
            suggested = "points"
        elif abs(rank) < abs(adp):
            suggested = "rank"
        else:
            suggested = "adp"
        out.append(TeamTendency(league.teams[t].name, n, auto_counts[t], top_rank, adp, rank, top_pts, suggested))
    return out


def draft_scores(state: DraftState) -> list[dict]:
    """Rolling score per team, by your (consensus) projections.

    "value": sum over the team's picks of (player's projected points − what
    that pick number is normally worth), where pick k is worth the k-th highest
    projection in the player pool. Positive = they've drafted more than their
    slots are worth (players falling to them, others' mistakes); negative =
    reaches. "espn_value": the same by ESPN's projections (what the room sees).
    "starters": projected points of their best starting lineup now.
    "auto_espn" / "auto_mine": their picks against what ESPN autodraft would
    have taken in their place (at each of their picks, ESPN's top projection
    still available), added up by ESPN's numbers and by yours. ESPN's is
    usually negative (anyone off ESPN's board loses by ESPN's numbers); yours
    positive means they beat autodraft by your projections. The other teams'
    picks are taken as they happened.
    """
    from .lineup import best_lineup

    league = state.league
    ps = [p for p in state.players.values() if not math.isnan(p.proj_points)]
    pool = sorted((p.proj_points for p in ps), reverse=True)
    espn_pool = sorted((p.room_points for p in ps), reverse=True)
    out = [{"value": 0.0, "espn_value": 0.0, "starters": 0.0, "auto_espn": 0.0, "auto_mine": 0.0}
           for _ in range(league.num_teams)]
    by_espn = sorted((p for p in ps if p.name != f"Unknown player {p.id}" and not p.id.startswith("unlisted-")),
                     key=lambda p: (-p.room_points, -p.proj_points))
    auto_took: list[set[str]] = [set() for _ in range(league.num_teams)]
    taken: set[str] = set()
    for k, pid in enumerate(state.picks):
        t = league.team_for_pick(k)
        p = state.players.get(pid)
        auto = next((q for q in by_espn if q.id not in taken and q.id not in auto_took[t]), None)
        taken.add(pid)
        if p is None or auto is None or p.name == f"Unknown player {p.id}":
            continue
        auto_took[t].add(auto.id)
        out[t]["auto_espn"] += p.room_points - auto.room_points
        out[t]["auto_mine"] += p.proj_points - auto.proj_points
    for k, pid in enumerate(state.picks):
        p = state.players.get(pid)
        if p is None or p.name == f"Unknown player {p.id}" or k >= len(pool):
            continue  # a pick we couldn't identify
        team = out[league.team_for_pick(k)]
        team["value"] += p.proj_points - pool[k]
        team["espn_value"] += p.room_points - espn_pool[k]  # the same, by ESPN's numbers
    for team, roster in enumerate(state.rosters()):
        starters, _ = best_lineup(roster, league.starting_slots)
        out[team]["starters"] = sum(p.proj_points for p in starters)
    return out
