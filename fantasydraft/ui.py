"""Draft dashboard: a local web page plus the draft-room feed, in one process.

    python -m fantasydraft ui --league league.json --picks picks.txt

Open http://127.0.0.1:8765 next to the ESPN draft tab. The browser userscript
posts the draft room's live feed to the same server (/log), so picks and
autodraft switches arrive by themselves. Without the userscript, enter picks
on the page. Recommendations are recomputed in a background thread whenever
the draft changes, so the page never blocks on a simulation.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import webbrowser
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .analyze import analyze, draft_scores
from .drafters import add_to_counts, full_positions
from .io import (apply_log, apply_manager, is_placeholder, league_managers, load_state, match_manager,
                 players_source)
from .learn import fit_teams
from .lineup import over_targets, target_fill
from .models import DraftState, Player, TeamProfile
from .recommend import MIN_AVAIL, availability_sims, recommend, versus_highest_projection
from .room import RoomTracker
from .simulate import availability, predict_picks

PAGE = Path(__file__).parent / "web" / "index.html"
TARGET_MIN_AVAIL = 0.5  # the target must have at least this chance of being there at your pick
GAMBLE_MIN_LASTS = 0.25  # only suggest waiting on the target if he has at least this chance of lasting
GAMBLE_MIN_GAIN = 25  # a GAMBLE must beat the best player likely left at your following pick by this much


def _pos(p: Player) -> str:
    return "/".join(sorted(p.positions))


def _agree(p: Player) -> list[int] | None:
    """[sources on the same side of ESPN as the consensus, other sources]."""
    others = [v for n, v in p.sources if n != "ESPN"]
    if not others:
        return None
    side = 1 if p.proj_points >= p.room_points else -1
    return [sum((v - p.room_points) * side > 0 for v in others), len(others)]


def _num(x: float) -> float | None:
    return None if x == float("inf") else x


class Dashboard:
    def __init__(self, players_src, league_path, picks_path, raw_log, sims: int, candidates: int):
        src = players_src or players_source(league_path)
        print(f"Players from: {src}", flush=True)
        state = load_state(players_src, league_path, None)
        self.league = state.league
        self.players = state.players
        ps = list(self.players.values())
        mine = sum(p.overridden for p in ps)
        mixed = sum(len(p.sources) > 1 for p in ps)
        print(f"  {len(ps)} players; {mine} with your my_proj; {mixed} with extra sources averaged in"
              + ("" if mixed else "  <- no extra sources: 'Mine' will equal ESPN") , flush=True)
        if not str(src).startswith("http"):
            print("  (a local file, not your Google Sheet: set \"players\" in the league file to the sheet link)", flush=True)
        print(f"  {self.league.num_teams} teams, you pick #{self.league.my_slot + 1}", flush=True)
        counts: dict[str, int] = {}
        for p in ps:
            for name, _ in p.sources:
                counts[name] = counts.get(name, 0) + 1
        self.source_names = sorted(counts, key=lambda n: -counts[n])  # "ESPN" first
        self.league_path = Path(league_path)
        self.managers = league_managers(league_path)
        self._tendencies: tuple[int, list] = (-1, [])
        self.tracker = RoomTracker(self.league, self.players, picks_path, log=self._log)
        self.raw_log = Path(raw_log) if raw_log else None
        self.sims, self.candidates = sims, candidates
        self.lock = threading.RLock()
        self.wake = threading.Condition(self.lock)
        self.result: dict = {"version": -1}
        self.done_version = -1  # last version fully computed
        self._prev_chances: tuple[int | None, dict] = (None, {})  # (my next pick, chances) last time
        self._prev: dict = {}  # chances to compare with in this round of updates
        self._likely: dict[str, float] = {}  # how often each player was the best left at my pick
        self._cum: dict[str, float] = {}  # P(the best left for me is this player or better)
        self._target: tuple[int | None, str | None] = (None, None)  # (my next pick, target id) last worked out
        self._suggested: dict[int, dict] = {}  # my pick (0-based) -> what was suggested last
        self._logged: set[int] = set()
        picks_file = Path(picks_path)
        self.decisions_path = picks_file.with_name(picks_file.stem + ".decisions.jsonl")
        self.computing = False
        self.predictions: list[dict] = []
        self.pred_history: dict[int, dict[str, float]] = {}  # pick -> {player: P} predicted beforehand
        self.page_connected: float | None = None
        threading.Thread(target=self._worker, daemon=True).start()

    def _log(self, line: str) -> None:
        print(line, flush=True)

    # --- state -----------------------------------------------------------------

    def state(self) -> DraftState:
        st = DraftState(self.league, dict(self.players))
        apply_log(st, self.tracker.entries)
        return st

    def _changed(self) -> None:
        with self.wake:
            self.wake.notify_all()

    def _publish(self, version: int, result: dict, stage: str) -> bool:
        """Show a result; False if a newer pick came in meanwhile (start over)."""
        with self.lock:
            if self.tracker.version != version:
                return False
            result.update(version=version, stage=stage)
            self.result = result
            return True

    def _worker(self) -> None:
        """After each change, in stages so the important part shows at once:
        1. instantly: who's available, ESPN vs your projections;
        2. ~1 s: chances each lasts to your pick, SAFE, the pick strip;
        3. then: the full comparison (vs SAFE, GAMBLE, advice)."""
        while True:
            with self.wake:
                while self.done_version == self.tracker.version:
                    self.wake.wait(timeout=5)
                version = self.tracker.version
                st = self.state()
                self.computing = True
            self._log_decisions(st)
            try:
                # Until the new chances are in, show last pick's (same next pick of
                # yours) so the availability lines don't vanish for a second.
                mine = st.my_next_picks()
                same = bool(mine) and self._prev_chances[0] == mine[0]
                old = self._prev_chances[1] if same else None
                self._cum = self._cumulative(st, self._likely if same else {})
                if not self._publish(version, self._quick(st, old), "quick"):
                    continue
                probs, outlook = None, None
                if not st.is_complete and st.my_next_picks():
                    likely: dict[str, float] = {}
                    probs = availability(st, sims=availability_sims(self.sims), seed=1, likely=likely)
                    nxt = st.my_next_picks()[0]
                    self._prev = self._prev_chances[1] if self._prev_chances[0] == nxt else {}
                    self._prev_chances = (nxt, probs)
                    self._likely = likely
                    self._cum = self._cumulative(st, likely)
                    outlook = self._outlook(st, probs, likely)
                    if not self._publish(version, {**self._quick(st, probs), "outlook": outlook}, "chances"):
                        continue
                self._predict(st)
                self._refresh_tendencies(st, version)  # cheap; don't wait for the slow stage
                if self.tracker.version != version:
                    continue
                result = {**self._compute(st, probs), "outlook": outlook}
            except Exception:
                traceback.print_exc()
                result = {"error": "recommendation failed — see terminal"}
            if not self._publish(version, result, "full"):
                continue
            with self.lock:
                self.computing = False
                self.done_version = version

    def _log_decisions(self, st: DraftState) -> None:
        """Once one of my picks is made, write down what was suggested for it."""
        lg = self.league
        for k, pid in enumerate(st.picks):
            if lg.team_for_pick(k) != lg.my_slot or k in self._logged or k not in self._suggested:
                continue
            self._logged.add(k)
            row = {"pick": k + 1, "took": pid, **self._suggested[k]}
            try:
                with self.decisions_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
            except OSError:
                pass

    def _refresh_tendencies(self, st: DraftState, version: int) -> None:
        try:
            rows = self._compute_tendencies(st)
            with self.lock:
                self._tendencies = (version, rows)
        except Exception:
            traceback.print_exc()

    def _predict(self, st: DraftState) -> None:
        """Likely players at each upcoming pick (for the pick strip), and a
        record of what was predicted for each pick so it can be scored later."""
        if st.is_complete:
            return
        mine = st.my_next_picks()
        horizon = (mine[1] if len(mine) > 1 else st.league.total_picks) - st.current_pick + 2
        try:
            preds = predict_picks(st, sims=150, horizon=max(min(horizon, 24), 6))
        except Exception:
            traceback.print_exc()
            return
        with self.lock:
            self.predictions = preds
            for row in preds:
                self.pred_history[row["pick"]] = dict(list(row["probs"].items())[:30])

    def _quick(self, st: DraftState, probs: dict | None = None) -> dict:
        """The fast part: top available players by your numbers (plus ESPN's top),
        and, once `probs` is known, their chances and the SAFE pick."""
        if st.is_complete or not st.my_next_picks():
            return {"recs": []}
        shown, espn_top = self._board(st, probs)
        # Keep the last target highlighted until the new comparison is in.
        mine = st.my_next_picks()
        keep = self._target[1] if self._target[0] == mine[0] else None
        gamble = self._gamble(st, probs, keep)
        rows = [{**self._board_row(st, p, probs),
                 "is_target": p.id == keep,
                 "tag": "gamble" if gamble and p.id == gamble["id"]
                        else "safe" if probs is not None and p is espn_top else None,
                 "is_baseline": probs is not None and p is espn_top} for p in shown]
        return {"recs": rows, "advice": {"gamble": gamble} if gamble else None, "has_sources": self._has_sources(st),
                "underrated": self._underrated(st, probs or {}), "floor_proj": self._floor_proj(st)}

    BOARD_SIZE = 14

    def _board(self, st: DraftState, probs: dict | None) -> tuple[list[Player], Player | None]:
        """The players to list: the best available by your projections (leaving
        out those almost sure to be gone before your pick), plus ESPN's pick."""
        on_clock = st.my_next_picks()[0] == st.current_pick
        avail = [p for p in st.available() if not is_placeholder(p)]
        if probs is not None and not on_clock:  # same cut as the recommendations
            avail = [p for p in avail if (probs.get(p.id) or [0])[0] >= MIN_AVAIL] or avail
        before_me = st.my_next_picks()[0] - st.current_pick
        shown = sorted(avail, key=lambda p: p.proj_points, reverse=True)[:max(self.BOARD_SIZE, before_me + 3)]
        espn_top = max(avail, key=lambda p: (p.room_points, p.proj_points), default=None)
        if espn_top is not None and espn_top not in shown:
            shown.append(espn_top)
            shown.sort(key=lambda p: -p.proj_points)  # in its place on the board, not at the end
        return shown, espn_top

    @staticmethod
    def _gamble(st: DraftState, probs: dict | None, target_id: str | None) -> dict | None:
        """GAMBLE: wait on the target. Take a player now who won't last to your
        following pick and beats the best player likely still there then (what
        you'd get at that pick if you took the target now), hoping the target
        lasts, so you end up with both. How safe it is: the chance the target lasts."""
        mine = st.my_next_picks()
        target = st.players.get(target_id) if target_id else None
        if probs is None or not mine or target is None or target.id in set(st.picks):
            return None
        on_clock = mine[0] == st.current_pick
        now = lambda p: 1.0 if on_clock else (probs.get(p.id) or [0])[0]  # noqa: E731
        lasts = lambda p: (probs.get(p.id) or [0, 0])[1] if len(probs.get(p.id) or []) > 1 else 0.0  # noqa: E731
        if len(mine) < 2:
            return None  # your last pick: nothing to wait for
        safety = lasts(target)
        if safety < GAMBLE_MIN_LASTS:
            return None  # the target almost surely won't last: there is nothing to wait on
        pool = [p for p in st.available() if not is_placeholder(p) and p.id != target.id]
        # Taking the target now leaves you, at your following pick, at best the
        # best player likely still there then. A gamble must beat that.
        later = max((p for p in pool if lasts(p) >= 0.5), key=lambda p: p.proj_points, default=None)
        bar = later.proj_points if later else float("-inf")
        options = [p for p in pool if now(p) >= 0.5 and lasts(p) < 0.5 and p.proj_points >= bar + GAMBLE_MIN_GAIN]
        if not options:
            return None
        g = max(options, key=lambda p: p.proj_points)
        return {"id": g.id, "name": g.name, "target": target.name, "target_id": target.id,
                "g_proj": round(g.proj_points), "t_proj": round(target.proj_points),
                "later": later.name if later else None, "gain": round(g.proj_points - bar) if later else None,
                "t_lasts": round(safety, 2), "g_lasts": round(lasts(g), 2),
                "level": "safe" if safety >= 0.7 else "medium" if safety >= 0.45 else "risky",
                "next_pick": mine[1] + 1}

    @staticmethod
    def _choose_target(recs: list, floor_pts: float | None):
        """recs come best plan first. The best plan is the target when he has an
        even or better chance of being there (it may be a player who won't last,
        taken now so a better one can be had later). If the best plan is a long
        shot: never a player below the floor (someone at least that good is
        guaranteed); among the rest the best plan with an even or better chance,
        else the one most likely to be there."""
        if recs and recs[0].p_avail_now >= TARGET_MIN_AVAIL:
            return recs[0]
        above = [r for r in recs if floor_pts is None or r.player.proj_points >= floor_pts] or recs
        return next((r for r in above if r.p_avail_now >= TARGET_MIN_AVAIL), None) \
            or max(above, key=lambda r: (r.p_avail_now, r.exp_final_value), default=None)

    @staticmethod
    def _cumulative(st: DraftState, likely: dict[str, float]) -> dict[str, float]:
        """For each available player, the chance that the best player left for
        you at your pick (by your projections) is this player or someone better."""
        acc, out = 0.0, {}
        for p in sorted((p for p in st.available() if not is_placeholder(p)),
                        key=lambda p: p.proj_points, reverse=True):
            acc += likely.get(p.id, 0.0)
            out[p.id] = round(min(acc, 1.0), 3)
        return out

    def _floor_proj(self, st: DraftState) -> int | None:
        fid = self._floor(st)
        return round(st.players[fid].proj_points) if fid else None

    @staticmethod
    def _floor(st: DraftState) -> str | None:
        """The guaranteed minimum: with N picks before yours, at least one of
        your N+1 best available players is still there whatever those teams
        take, so you get someone at least as good as the (N+1)-th."""
        before_me = st.my_next_picks()[0] - st.current_pick
        ranked = sorted((p for p in st.available() if not is_placeholder(p)),
                        key=lambda p: p.proj_points, reverse=True)
        return ranked[before_me].id if before_me < len(ranked) else None

    def _board_row(self, st: DraftState, p: Player, probs: dict | None) -> dict:
        on_clock = st.my_next_picks()[0] == st.current_pick
        pr = (probs or {}).get(p.id, [])
        return {**self._player_fields(p), **self._trend(st, p, probs), "is_floor": p.id == self._floor(st),
                "p_best_up": self._cum.get(p.id),
                "p_now": 1.0 if on_clock else (pr[0] if pr else None) if probs is not None else None,
                "p_after": (pr[1] if len(pr) > 1 else 0.0) if probs is not None else None}

    @staticmethod
    def _outlook(st: DraftState, probs: dict, likely: dict[str, float]) -> dict | None:
        """When you're not on the clock: who is likely the best player left (by
        your projections) at your next pick."""
        mine = st.my_next_picks()
        if not mine or mine[0] == st.current_pick:
            return None
        pick = mine[0] + 1

        def brief(p: Player, chance: float) -> dict:
            rank = p.espn_rank if math.isfinite(p.espn_rank) else None
            return {"name": p.name, "pos": _pos(p), "p": round(chance, 2), "proj": round(p.proj_points),
                    "espn": round(p.room_points), "rank": None if rank is None else int(rank)}

        top = sorted(likely.items(), key=lambda kv: -kv[1])[:4]
        return {"pick": pick, "away": mine[0] - st.current_pick,
                "likely": [brief(st.players[pid], c) for pid, c in top if c >= 0.05]}

    def _trend(self, st: DraftState, p: Player, probs: dict | None) -> dict:
        """Whether a player is falling to you (ESPN ranks them 3+ picks before
        your pick, yet they have a real chance of lasting), and how their chance
        of lasting changed since the last pick."""
        mine = st.my_next_picks()
        if probs is None or not mine or mine[0] == st.current_pick:
            return {}
        now = (probs.get(p.id) or [0])[0]
        before = (self._prev.get(p.id) or [None])[0]
        return {"falling": math.isfinite(p.espn_rank) and p.espn_rank <= mine[0] + 1 - 3 and now >= 0.15,
                "p_change": None if before is None else round(now - before, 2)}

    @staticmethod
    def _player_fields(p: Player) -> dict:
        return {"id": p.id, "name": p.name, "pos": _pos(p), "team": p.nba_team,
                "proj": round(p.proj_points), "espn": round(p.room_points),
                "vs_espn": round(p.proj_points - p.room_points),
                "sources": [[n, round(v)] for n, v in p.sources], "agree": _agree(p)}

    @staticmethod
    def _has_sources(st: DraftState) -> bool:
        return any(len(p.sources) > 1 for p in st.players.values())

    def _compute(self, st: DraftState, probs: dict | None = None) -> dict:
        if st.is_complete or not st.my_next_picks():
            return {"recs": []}
        # One availability run, shared by the recommendations and the advice.
        if probs is None:
            probs = availability(st, sims=availability_sims(self.sims), seed=1)
        recs = recommend(st, candidates=self.candidates, sims=self.sims, avail=probs)
        # The target: never below the floor (someone at least that good is
        # guaranteed); among those, the best plan with an even or better chance of
        # being there, else the one most likely to be there.
        floor_id = self._floor(st)
        realistic = self._choose_target(recs, st.players[floor_id].proj_points if floor_id else None)
        if realistic is not None:
            recs = [realistic] + [r for r in recs if r is not realistic]
        advice = self._advice(st, recs, probs)
        baseline, vs = versus_highest_projection(recs)
        gamble = self._gamble(st, probs, recs[0].player.id if recs else None)
        if advice is not None:
            advice["safe"] = baseline.name if baseline else None
            advice["gamble"] = gamble
            # Remember what was suggested for this pick of yours, for `review` afterwards.
            self._suggested[st.my_next_picks()[0]] = {
                "target": recs[0].player.id if recs else None,
                "espn": baseline.id if baseline else None,
                "gamble": gamble["id"] if gamble else None,
                "gamble_level": gamble["level"] if gamble else None,
                "gamble_t_lasts": gamble["t_lasts"] if gamble else None}

        def vs_fields(r):
            v = vs.get(r.player.id)
            if v is None:
                return {}
            return {"vs_top": round(v.diff), "is_baseline": baseline is not None and r.player.id == baseline.id,
                    "p_better": v.p_better, "avg_up": round(v.avg_up), "p_worse": v.p_worse,
                    "avg_down": round(v.avg_down)}

        floor = self._floor(st)
        rows = [{
            **self._player_fields(r.player), **self._trend(st, r.player, probs),
            "p_now": r.p_avail_now, "p_after": r.p_avail_after,
            "value": round(r.exp_final_value),
            **vs_fields(r),
            "is_target": i == 0, "is_floor": r.player.id == floor, "p_best_up": self._cum.get(r.player.id),
            "tag": "gamble" if gamble and r.player.id == gamble["id"]
                   else "safe" if baseline and r.player.id == baseline.id else None,
        } for i, r in enumerate(recs)]
        if recs:
            self._target = (st.my_next_picks()[0], recs[0].player.id)
        # The rest of the board (not compared in the simulations), all by your projections.
        listed = {r.player.id for r in recs}
        board, _ = self._board(st, probs)
        rows += [{**self._board_row(st, p, probs), "tag": "gamble" if gamble and p.id == gamble["id"] else None}
                 for p in board if p.id not in listed]
        rows.sort(key=lambda x: -x["proj"])
        return {
            "advice": advice,
            "recs": rows,
            "underrated": self._underrated(st, probs),
            "has_sources": self._has_sources(st),
            "floor_proj": self._floor_proj(st),
        }

    @staticmethod
    def _advice(st: DraftState, recs, probs: dict | None = None) -> dict | None:
        """Plain-language note on whether to take ESPN's top-projected player (SAFE)."""
        if not recs:
            return None
        top = recs[0]
        best = max(recs, key=lambda r: (r.player.room_points, r.player.proj_points))  # ESPN's top
        mine = st.my_next_picks()
        on_clock = mine[0] == st.current_pick
        now_pick = mine[0] + 1
        later = mine[1] + 1 if len(mine) > 1 else None
        if best.player.id == top.player.id:
            return {"kind": "same", "top": top.player.name, "pick": now_pick,
                    "top_p": top.p_avail_now, "away": mine[0] - st.current_pick}
        gain = round(top.exp_final_value - best.exp_final_value)
        reasons = []
        if later and best.p_avail_after - top.p_avail_after >= 0.2:
            reasons.append(f"{best.player.name} is {best.p_avail_after:.0%} likely to still be there at "
                           f"#{later}; {top.player.name} only {top.p_avail_after:.0%}")
        if later and probs:
            # Position drop-off (value over next available): who is likely left later
            # at each player's position if you take the other one now.
            def likely_left(player, skip):
                pool = [q for q in st.available() if q.id not in skip and q.positions & player.positions
                        and len(probs.get(q.id, [])) > 1 and probs[q.id][1] >= 0.5]
                return max(pool, key=lambda q: q.proj_points, default=None)

            skip = {top.player.id, best.player.id}
            alt_best, alt_top = likely_left(best.player, skip), likely_left(top.player, skip)
            if alt_best and alt_top:
                drop_best = best.player.proj_points - alt_best.proj_points
                drop_top = top.player.proj_points - alt_top.proj_points
                if drop_top > drop_best + 100:
                    pos_b = "/".join(sorted(best.player.positions))
                    pos_t = "/".join(sorted(top.player.positions))
                    reasons.append(f"{pos_b} stays deep: {alt_best.name} ({alt_best.proj_points:,.0f}) should still be "
                                   f"there at #{later}. At {pos_t} the drop is steep: after {top.player.name} the best "
                                   f"likely left is {alt_top.name} ({alt_top.proj_points:,.0f})")
        roster = st.rosters()[st.league.my_slot]
        if best.player in over_targets(roster + [best.player], st.league):
            pos = "/".join(sorted(best.player.positions))
            reasons.append(f"{best.player.name} would be over your {pos} target")
        elif top.marginal_now > best.marginal_now + 1:
            reasons.append(f"{top.player.name} adds more to your lineup now "
                           f"(+{top.marginal_now:.0f} vs +{best.marginal_now:.0f})")
        if not on_clock and best.p_avail_now < 0.5:
            reasons.append(f"{best.player.name} is only {best.p_avail_now:.0%} likely to reach #{now_pick}")
        if not reasons:
            reasons.append("the simulated drafts favour it (timing and roster fit)")
        return {"kind": "close" if gain < 40 else "different", "top": top.player.name,
                "best": best.player.name, "best_proj": round(best.player.room_points),
                "top_proj": round(top.player.proj_points), "gain": gain, "reasons": reasons,
                "pick": now_pick, "top_p": top.p_avail_now, "best_p": best.p_avail_now,
                "away": mine[0] - st.current_pick}

    def _underrated(self, st: DraftState, probs: dict) -> list[dict]:
        """Available players the consensus ranks well above ESPN (the room's view)."""
        avail = [p for p in st.available() if not is_placeholder(p) and len(p.sources) > 1]
        if not avail:
            return []
        mine = {p.id: i + 1 for i, p in enumerate(sorted(avail, key=lambda p: -p.proj_points))}
        room = {p.id: i + 1 for i, p in enumerate(sorted(avail, key=lambda p: -p.room_points))}
        rows = [p for p in avail if mine[p.id] <= 60 and room[p.id] - mine[p.id] >= 2]
        rows.sort(key=lambda p: mine[p.id] - room[p.id])
        return [{
            "id": p.id, "name": p.name, "pos": _pos(p), "team": p.nba_team,
            "proj": round(p.proj_points), "espn": round(p.room_points), "my_rank": mine[p.id],
            "espn_rank": room[p.id], "spread": round(p.spread),
            "sources": [[n, round(v)] for n, v in p.sources], "p": probs.get(p.id, [])[:3],
            "agree": _agree(p),
        } for p in rows[:25]]

    def snapshot(self) -> dict:
        # Only quick reads under the lock: the draft feed waits on it.
        with self.lock:
            tr = self.tracker
            st = self.state()
            lg = self.league
            version = tr.version
            names = [tr.name(i) for i in range(lg.num_teams)]
            computing = self.computing or self.result["version"] != version
            feed = {
                "connected": self.page_connected is not None,
                "last_message_age": None if tr.last_feed is None else time.time() - tr.last_feed,
                "blocked": tr.blocked,
                "missing": tr.missing(),
                "team_count": tr.team_count,
                "file_teams": lg.num_teams,
                "my_position": tr.my_draft_position(),
                "league_file": self.league_path.name,
                "picks_file": tr.path.name,
            }
            seconds_left = tr.seconds_left()
            log = list(tr.recent_log)[-12:]
            result = self._current_result(st)
            strip = self._strip(st, result)
            tendencies = self._tendencies[1] or [None] * lg.num_teams
        pick = st.current_pick
        mine = st.my_next_picks()
        rosters = st.rosters()
        scores = draft_scores(st)
        on_clock = lg.team_for_pick(pick) if not st.is_complete else None
        filled, over = target_fill(rosters[lg.my_slot], lg)
        team_counts = []
        for roster in rosters:
            counts: dict[str, float] = {}
            for p in roster:
                add_to_counts(counts, p)
            team_counts.append(counts)
        return {
            "version": version,
            "computing": computing,
            "status": {
                "pick": pick + 1, "round": pick // lg.num_teams + 1, "rounds": lg.rounds,
                "teams": lg.num_teams, "complete": st.is_complete,
                "on_clock": None if on_clock is None else {"slot": on_clock, "name": names[on_clock]},
                "my_next": mine[0] + 1 if mine else None,
                "picks_until": mine[0] - pick if mine else None,
                "my_picks": [m + 1 for m in mine],
                "seconds_left": seconds_left,
            },
            "feed": feed,
            "me": {"slot": lg.my_slot, "name": names[lg.my_slot]},
            "sources": self.source_names,
            "teams": [{
                "score": round(scores[i]["value"]), "espn_score": round(scores[i]["espn_value"]),
                "starters": round(scores[i]["starters"]),
                "starters_rank": 1 + sum(s["starters"] > scores[i]["starters"] for s in scores),
                "slot": i, "name": names[i], "strategy": t.strategy, "noise": t.noise,
                "manager": t.manager, "view": t.view, "favorite_team": t.favorite_team,
                "auto": i in st.auto_teams, "me": i == lg.my_slot, "count": len(rosters[i]),
                "last": rosters[i][-1].name if rosters[i] else None,
                "observed": tendencies[i] if i < len(tendencies) else None,
                "full": sorted(full_positions(team_counts[i], lg.targets)),
            } for i, t in enumerate(lg.teams)],
            "positions": {"targets": lg.targets, "filled": filled,
                          "over": [p.name for p in over]},
            "strip": strip,
            "log": log,
            "result": result,
        }

    def _strip(self, st: DraftState, result: dict) -> dict:
        """Pick strip: recent picks scored against what was predicted, then the
        pick on the clock and upcoming picks with their most likely players."""
        lg, tr = self.league, self.tracker
        pick = st.current_pick

        def brief(pid: str) -> dict:
            p = st.players[pid]
            return {"name": p.name, "pos": _pos(p)}

        def before(i: int) -> dict[str, float] | None:
            """The prediction for pick i, given the picks made before it: the
            prediction may have been made a few picks earlier (fast rooms), so
            players taken since are dropped and the rest scaled back up."""
            pred = self.pred_history.get(i)
            if not pred:
                return pred
            gone = set(st.picks[:i])
            left = {k: v for k, v in pred.items() if k not in gone}
            total = sum(left.values())
            return {k: v / total for k, v in left.items()} if total > 0 else {}

        past = []
        for i in range(max(0, pick - 4), pick):
            pid, slot = st.picks[i], lg.team_for_pick(i)
            pred = before(i)
            guess = next(iter(pred.items()), None) if pred else None
            past.append({"pick": i + 1, "team": tr.name(slot), "mine": slot == lg.my_slot,
                         "auto": i in st.auto_picks, **brief(pid),
                         "p": None if pred is None or slot == lg.my_slot else pred.get(pid, 0.0),
                         "guess": None if guess is None else {**brief(guess[0]), "p": guess[1]}})
        upcoming = []
        # The top recommendation once the full comparison is in (already without anyone just drafted).
        top_rec = next((x for x in result.get("recs") or [] if x.get("is_target")), None) \
            if result.get("stage") == "full" else None
        taken = set(st.picks)
        taken_names = {st.players[pid].name for pid in taken if pid in st.players}
        for row in self.predictions:
            if row["pick"] < pick:
                continue
            slot = row["team"]
            upcoming.append({
                "pick": row["pick"] + 1, "team": tr.name(slot), "mine": slot == lg.my_slot,
                "auto": slot in st.auto_teams, "now": row["pick"] == pick,
                "likely": [{**brief(pid), "p": p} for pid, p in row["probs"].items() if pid not in taken][:3],
                "rec": top_rec["name"] if slot == lg.my_slot and top_rec and row["pick"] == st.my_next_picks()[0] else None,
                "best_left": [x for x in (result.get("outlook") or {}).get("likely", [])
                              if x["name"] not in taken_names][:3]
                             if slot == lg.my_slot and row["pick"] == st.my_next_picks()[0] else None,
            })
        # How well the model has predicted opponents' picks so far.
        scored = [(before(i), pid, i in st.auto_picks) for i, pid in enumerate(st.picks)
                  if i in self.pred_history and lg.team_for_pick(i) != lg.my_slot]

        def rates(rows):
            if not rows:
                return None
            return {"picks": len(rows),
                    "top_hit": sum(bool(pr) and next(iter(pr)) == pid for pr, pid, _ in rows) / len(rows),
                    "top3_hit": sum(pid in list(pr)[:3] for pr, pid, _ in rows) / len(rows),
                    "avg_p": sum(pr.get(pid, 0.0) for pr, pid, _ in rows) / len(rows)}
        accuracy = None
        if scored:
            accuracy = {**rates(scored), "people": rates([r for r in scored if not r[2]]),
                        "auto": rates([r for r in scored if r[2]])}
        return {"past": past, "upcoming": upcoming, "accuracy": accuracy}

    def _compute_tendencies(self, st: DraftState) -> list[dict | None]:
        """What each team's own (non-autodraft) picks so far suggest (run by the worker)."""
        fits = fit_teams(st) if st.picks else {}
        rows = []
        for i, t in enumerate(analyze(st)):
            f = fits.get(i)
            if t.picks == 0:
                rows.append(None)
                continue
            best = max(f.fit, key=f.fit.get) if f else None
            rows.append({
                "picks": t.picks, "auto_picks": t.auto_picks,
                "top_rank": round(t.top_rank_rate, 2), "vs_adp": round(t.reach_vs_adp, 1),
                "fit": {k: round(v, 2) for k, v in f.fit.items()} if f else None,
                "best": best, "best_p": round(f.fit[best], 2) if f else None,
                "noise": f.noise if f else None, "prior_noise": f.prior_noise if f else None,
                "used": f.strategy if f else None,
                "mix": [[s, round(p, 2)] for s, (p, _) in f.mix.items()] if f else None,
            })
        return rows

    def set_strategy(self, slot: int, strategy: str) -> str:
        """Change a team's saved strategy, here and in the league file."""
        lg = self.league
        if not 0 <= slot < lg.num_teams:
            raise ValueError("no such team")
        if strategy not in TeamProfile.VALID_STRATEGIES or strategy == "auto":
            raise ValueError(f"unknown strategy {strategy!r}")
        lg.teams[slot] = replace(lg.teams[slot], strategy=strategy)
        cfg = json.loads(self.league_path.read_text(encoding="utf-8-sig"))
        cfg["teams"][slot]["strategy"] = strategy
        self.league_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        self.tracker.version += 1  # re-run recommendations with the new model
        name = self.tracker.name(slot)
        self.tracker.log(f"   {name}: strategy set to {strategy}")
        return f"{name} now modelled as '{strategy}'"

    def _current_result(self, st: DraftState) -> dict:
        """The latest recommendations, minus anyone drafted since they were computed
        (a new pick arrives seconds before the recompute finishes)."""
        r = self.result
        if r.get("version") == self.tracker.version:
            return r
        taken = set(st.picks)
        out = dict(r, stale=True)
        for key in ("recs", "available", "underrated"):
            if isinstance(r.get(key), list):
                out[key] = [x for x in r[key] if x.get("id") not in taken]
        adv = r.get("advice")
        if adv and any(st.players.get(pid) and st.players[pid].name in (adv.get("top"), adv.get("best"))
                       for pid in taken):
            out["advice"] = None  # it was about a player who's now gone
        return out

    def players_list(self) -> list[dict]:
        with self.lock:
            taken = set(self.tracker.picks)
        return [{"id": p.id, "name": p.name, "pos": _pos(p), "team": p.nba_team,
                 "proj": round(p.proj_points), "rank": _num(p.espn_rank)}
                for p in self.players.values() if p.id not in taken and not is_placeholder(p)]

    # --- actions ---------------------------------------------------------------

    def action(self, path: str, body: dict) -> dict:
        with self.lock:
            tr = self.tracker
            if path == "/api/pick":
                if body.get("id"):
                    msg = tr.manual_pick(str(body["id"]))
                else:  # someone not in your players list
                    msg = tr.manual_pick(name=str(body.get("name", "")).strip() or None)
            elif path == "/api/undo":
                msg = "undid " + tr.undo()
            elif path == "/api/auto":
                tr.manual_auto(int(body["slot"]), bool(body["on"]))
                msg = "ok"
            elif path == "/api/strategy":
                msg = self.set_strategy(int(body["slot"]), str(body["strategy"]))
            else:
                raise KeyError(path)
        self._changed()
        return {"ok": True, "message": msg}

    def feed(self, events: list[dict]) -> None:
        with self.lock:
            if self.raw_log:
                with self.raw_log.open("a", encoding="utf-8") as f:
                    for e in events:
                        f.write(json.dumps(e) + "\n")
            before = self.tracker.version
            for e in events:
                kind = e.get("kind")
                if kind == "ws-in":
                    self.tracker.handle_message(str(e.get("data", "")))
                elif kind == "dom-text":
                    self.tracker.handle_page_text(str(e.get("data", "")))
                elif kind == "hello":
                    self.page_connected = time.time()
                    print(f"Draft page connected: {e.get('url', '')}", flush=True)
            self._match_managers()
            changed = self.tracker.version != before
        if changed:
            self._changed()

    def _match_managers(self) -> None:
        """Attach people's profiles to teams by the team names on the draft page."""
        if not self.managers:
            return
        lg = self.league
        for slot, name in self.tracker.slot_names.items():
            team = lg.teams[slot]
            if slot == lg.my_slot or team.manager or team.strategy != "unknown":
                continue
            person = match_manager(self.managers, name, team.name)
            if person:
                lg.teams[slot] = apply_manager(team, self.managers, person)
                self.tracker.version += 1
                self.tracker.log(f"   {name}: using {person}'s profile ({lg.teams[slot].strategy})")


def _app_browser() -> str | None:
    """Chrome or Edge executable, for a window without tabs or address bar."""
    for name in ("chrome", "google-chrome", "chromium", "msedge", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    candidates = []
    for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
        if base:
            candidates += [os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
                           os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")]
    candidates += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                   "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    return next((c for c in candidates if os.path.exists(c)), None)


def open_window(url: str, app_window: bool = True) -> None:
    """Open the dashboard: its own app-style window if possible, else a normal tab."""
    exe = _app_browser() if app_window else None
    if exe:
        try:
            subprocess.Popen([exe, f"--app={url}", "--window-size=900,1000"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            pass
    webbrowser.open(url)


def serve(dash: Dashboard, port: int = 8765, open_browser: bool = True, app_window: bool = True) -> None:
    class Handler(BaseHTTPRequestHandler):
        def _origin_ok(self, page_feed: bool) -> bool:
            """Only the dashboard itself may use the API; the draft feed may also
            come from ESPN's pages (via the userscript). Other websites may not."""
            origin = self.headers.get("Origin")
            if not origin or origin == "null":
                return True  # not from a web page (the userscript's requests, curl)
            own = (f"http://127.0.0.1:{port}", f"http://localhost:{port}")
            if origin in own or origin.startswith(("chrome-extension://", "moz-extension://")):
                return True
            return page_feed and re.match(r"https://([a-z0-9-]+\.)*espn\.com$", origin) is not None

        def _send(self, code: int, body: bytes = b"", ctype: str = "application/json") -> None:
            self.send_response(code)
            origin = self.headers.get("Origin")
            if origin and self._origin_ok(page_feed=True):
                self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Cache-Control", "no-store")
            if body:
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode())

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._send(204)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(dash.snapshot())
            elif path == "/api/players":
                self._json(dash.players_list())
            elif path == "/favicon.ico":
                self._send(204)
            else:
                self._send(404)

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            if not self._origin_ok(page_feed=path == "/log"):
                self._send(403)
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._json({"ok": False, "error": "bad JSON"}, 400)
                return
            if path == "/log":  # from the userscript
                dash.feed(body if isinstance(body, list) else [body])
                self._send(204)
                return
            try:
                self._json(dash.action(path, body))
            except KeyError:
                self._send(404)
            except ValueError as e:
                self._json({"ok": False, "error": str(e)}, 400)

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"Dashboard: {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: open_window(url, app_window)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
