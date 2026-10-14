"""After a draft: did going off ESPN's board pay off?

For each of your picks: what you took, what ESPN's board would have taken
(its top projection still available), what the dashboard suggested (if it
kept a record), and whether ESPN's player was still there at your next pick.
Then your final roster against an "ESPN autodraft you" who always took ESPN's
top available player, valued both by your combined projections and by ESPN's.

The other teams' picks are taken as they happened, so the autodraft roster is
an approximation (someone it took might have changed a later pick). And in a
mock, "paid off" can only mean "by projections": whether your sources were
right only shows in real games.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from .io import is_placeholder
from .lineup import assign_lineup, over_targets, team_value
from .models import DraftState


def _breakdown(label: str, roster: list, lg) -> str:
    owners, bench = assign_lineup(roster, lg.starting_slots)
    starters = sum(p.proj_points for p in owners if p is not None)
    bench_pts = sum(p.proj_points for p in bench)
    over = over_targets(roster, lg)
    penalty = lg.balance_penalty * sum(p.proj_points for p in over)
    total = starters + lg.bench_weight * bench_pts - penalty
    return (f"  {label:<36} starters {starters:>7,.0f} + bench {bench_pts:>6,.0f} x {lg.bench_weight:g}"
            f" - over targets {penalty:>5,.0f} = {total:>7,.0f}   (all 13 added up: {starters + bench_pts:,.0f}; "
            f"bench: {', '.join(p.name for p in bench) or '-'}; over: {', '.join(p.name for p in over) or '-'})")


def review(state: DraftState, decisions_path: str | Path | None = None) -> str:
    lg = state.league
    players = state.players
    picks = state.picks
    mine = [k for k in range(len(picks)) if lg.team_for_pick(k) == lg.my_slot]
    suggested = {}
    if decisions_path and Path(decisions_path).exists():
        for line in Path(decisions_path).read_text(encoding="utf-8-sig").splitlines():
            try:
                row = json.loads(line)
                suggested[row["pick"] - 1] = row
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
    name = lambda pid: players[pid].name if pid in players else "?"  # noqa: E731

    out = [f"{len(picks)} picks, {lg.num_teams} teams, you picked #{lg.my_slot + 1}"
           + ("" if suggested else " (no record of the dashboard's suggestions for this draft)"), ""]
    cf: list = []  # the "ESPN autodraft you" roster
    gambles = followed = 0
    for i, k in enumerate(mine):
        before = set(picks[:k])
        pool = [p for p in players.values() if p.id not in before and not is_placeholder(p)]
        espn = max(pool, key=lambda p: (p.room_points, p.proj_points), default=None)
        took = players[picks[k]]
        cf_pick = max((p for p in pool if p.id not in {q.id for q in cf}),
                      key=lambda p: (p.room_points, p.proj_points), default=None)
        if cf_pick:
            cf.append(cf_pick)
        pos = "/".join(sorted(took.positions))
        line = (f"#{k + 1:>3} took {took.name} {pos} (mine {took.proj_points:,.0f} / ESPN {took.room_points:,.0f})")
        if espn and espn.id != took.id:
            nxt = mine[i + 1] if i + 1 < len(mine) else None
            if nxt is None:
                fate = ""
            elif espn.id in picks[k + 1:nxt]:
                gone_at = picks.index(espn.id, k + 1) + 1
                fate = f"; gone at #{gone_at}, before your next pick"
            else:
                fate = f"; still there at your next pick #{nxt + 1}"
            line += f" | ESPN's pick: {espn.name} (mine {espn.proj_points:,.0f} / ESPN {espn.room_points:,.0f}){fate}"
        else:
            line += " | = ESPN's pick"
        s = suggested.get(k)
        if s:
            tags = []
            if s.get("gamble"):
                if s.get("gamble_level"):
                    detail = f" ({s['gamble_level']}, {s['gamble_gain']:+} by your projections)"
                elif s.get("gamble_p_better") is not None:  # records from older versions
                    detail = f" ({s['gamble_p_better']:.0%} chance better, {s['gamble_diff']:+})"
                else:
                    detail = ""
                tags.append(f"GAMBLE was {name(s['gamble'])}{detail}")
                gambles += s["gamble"] == took.id
            if s.get("target"):
                tags.append(f"target was {name(s['target'])}")
                followed += s["target"] == took.id
            line += "  [" + "; ".join(tags) + "]"
        out.append(line)

    roster = [players[picks[k]] for k in mine]
    counts = {pos: sum(pos in p.positions for p in roster) for pos in ("PG", "SG", "SF", "PF", "C")}
    out += ["", "Your roster by position (players eligible): "
                + "  ".join(f"{pos} {n}" for pos, n in counts.items())]
    by_espn = lambda r: [replace(p, proj_points=p.room_points) for p in r]  # noqa: E731
    mine_v, cf_v = team_value(roster, lg), team_value(cf, lg)
    mine_e, cf_e = team_value(by_espn(roster), lg), team_value(by_espn(cf), lg)
    out += ["", "Your roster vs always taking ESPN's top available player:",
            f"  by your combined projections: {mine_v:,.0f} vs {cf_v:,.0f}  ({mine_v - cf_v:+,.0f})",
            f"  by ESPN's projections:        {mine_e:,.0f} vs {cf_e:,.0f}  ({mine_e - cf_e:+,.0f})"]
    if suggested:
        out.append(f"  you took the target {followed} of {len(suggested)} times, a GAMBLE {gambles} times")
    out += ["", "How those numbers are made (H2H points: starters count in full, the bench at "
                f"{lg.bench_weight:.0%}, players beyond your position targets lose {lg.balance_penalty:.0%}):"]
    for label, r in (("you, by your projections", roster), ("ESPN-autodraft, by your projections", cf),
                     ("you, by ESPN's", by_espn(roster)), ("ESPN-autodraft, by ESPN's", by_espn(cf))):
        out.append(_breakdown(label, r, lg))
    out += ["", "If the first number is + and the second -, your picks are a bet that your sources beat "
                "ESPN's numbers; whether they do only shows in real games.",
            "ESPN-autodraft roster: " + ", ".join(p.name for p in cf)]
    return "\n".join(out)
