"""How do the drafters in your mocks actually pick? Pooled over many drafts.

For every opponent pick, made by a person or by autodraft, it looks at where
the player taken sat among the players still available on each list the
model can follow (ESPN's rank, ADP, ESPN's projections, your projections),
whether the team skipped positions it had already filled, and how far picks
stray from each list early, in the middle and late in the draft. No
simulation, so it's quick, and the output is short enough to paste.
"""
from __future__ import annotations

import statistics
from pathlib import Path

from .drafters import add_to_counts, base_key, full_positions
from .io import is_placeholder
from .models import DraftState

LISTS = {"ESPN rank": "rank", "ADP": "adp", "ESPN proj": "points@espn", "your proj": "points@consensus"}
BUCKETS = ((1, 1, "#1"), (2, 2, "#2"), (3, 3, "#3"), (4, 5, "4-5"), (6, 10, "6-10"), (11, 10**9, "11+"))


def _rank_among(pid: str, pool: list, key: str) -> int:
    """1-based place of `pid` among `pool` sorted by the list `key`."""
    mine = base_key(key, next(p for p in pool if p.id == pid))
    return 1 + sum(base_key(key, p) < mine for p in pool)


def _phase(k: int, total: int) -> str:
    return "early" if k < total / 3 else "middle" if k < 2 * total / 3 else "late"


def study(drafts: list[tuple[str, DraftState]]) -> str:
    groups = ("autodraft", "people")
    places = {g: {name: [] for name in LISTS} for g in groups}  # where the pick sat on each list
    any_top = {g: [0, 0] for g in groups}  # [#1 on at least one list, picks]
    need = {g: [0, 0, 0.0] for g in groups}  # [picks at filled positions, picks, expected if need didn't matter]
    phase_place = {g: {ph: {name: [] for name in LISTS} for ph in ("early", "middle", "late")} for g in groups}
    per_team: dict[tuple[str, int], dict[str, list[int]]] = {}
    lines = []
    for label, st in drafts:
        lg = st.league
        targets = lg.targets
        counts: list[dict[str, float]] = [{} for _ in lg.teams]
        taken: set[str] = set()
        n = 0
        for k, pid in enumerate(st.picks):
            team = lg.team_for_pick(k)
            p = st.players.get(pid)
            if p is None:
                continue
            if team != lg.my_slot and not is_placeholder(p):
                g = "autodraft" if k in st.auto_picks else "people"
                pool = [q for q in st.players.values() if q.id not in taken and not is_placeholder(q)]
                n += 1
                any_top[g][1] += 1
                top = False
                for name, key in LISTS.items():
                    r = _rank_among(pid, pool, key)
                    places[g][name].append(r)
                    phase_place[g][_phase(k, lg.total_picks)][name].append(r)
                    if g == "people":
                        per_team.setdefault((label, team), {}).setdefault(name, []).append(r)
                    top |= r == 1
                any_top[g][0] += top
                full = full_positions(counts[team], targets)
                if full:
                    near = sorted(pool, key=lambda q: base_key("adp", q))[:6]
                    need[g][0] += p.positions <= full
                    need[g][1] += 1
                    need[g][2] += sum(q.positions <= full for q in near) / len(near)
            add_to_counts(counts[team], p)
            taken.add(pid)
        lines.append(f"  {label}: {lg.num_teams} teams, {n} opponent picks "
                     f"({sum(1 for k in st.auto_picks if lg.team_for_pick(k) != lg.my_slot)} by autodraft)")

    out = [f"{len(drafts)} drafts", *lines, ""]
    for g in groups:
        total = any_top[g][1]
        if not total:
            continue
        out.append(f"{g.upper()} ({total} picks): where the player taken sat among those still available")
        out.append(f"  {'':12}" + "".join(f"{b[2]:>6}" for b in BUCKETS) + f"{'median':>8}")
        for name in LISTS:
            rs = places[g][name]
            row = "".join(f"{sum(lo <= r <= hi for r in rs) / len(rs):>6.0%}" for lo, hi, _ in BUCKETS)
            out.append(f"  {name:12}{row}{statistics.median(rs):>8g}")
        out.append(f"  #1 on at least one of these lists: {any_top[g][0] / total:.0%}")
        out.append("  median place by stage of the draft:  " + "   ".join(
            f"{ph} " + "/".join(f"{statistics.median(v[name]):g}" if v[name] else "-" for name in LISTS)
            for ph, v in phase_place[g].items()) + "   (" + "/".join(LISTS) + ")")
        filled, n_full, expect = need[g]
        if n_full:
            out.append(f"  position need: when a team had filled a position, {filled / n_full:.0%} of its picks were "
                       f"there anyway (vs {expect / n_full:.0%} of the players near the top of ADP)  [{n_full} picks]")
        out.append("")

    out.append("PEOPLE, team by team (median place on each list; lowest = the list they follow):")
    for (label, team), lists in per_team.items():
        st = next(s for lb, s in drafts if lb == label)
        if len(lists["ADP"]) < 4:
            continue
        meds = {name: statistics.median(v) for name, v in lists.items()}
        best = min(meds, key=meds.get)
        out.append(f"  {label} {st.league.teams[team].name[:16]:16} " +
                   " ".join(f"{name} {m:g}" for name, m in meds.items()) + f"  -> {best}")
    return "\n".join(out)


def find_drafts(folder: str = "mocks") -> list[tuple[Path, Path]]:
    """Every picks file in `folder` that has its league file saved next to it."""
    out = []
    for picks in sorted(Path(folder).glob("*.txt")):
        if ".old-" in picks.name:
            continue
        league = picks.with_name(picks.stem + ".league.json")
        if league.exists():
            out.append((picks, league))
    return out
