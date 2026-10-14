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
import os
import shutil
import subprocess
import threading
import time
import traceback
import webbrowser
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .analyze import analyze
from .drafters import add_to_counts, full_positions
from .io import apply_log, apply_manager, is_placeholder, league_managers, load_state, match_manager
from .learn import fit_teams
from .lineup import assign_lineup, over_targets, target_fill
from .models import DraftState, Player, TeamProfile
from .recommend import recommend
from .room import RoomTracker
from .simulate import availability, predict_picks

PAGE = Path(__file__).parent / "web" / "index.html"


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
        state = load_state(players_src, league_path, None)
        self.league = state.league
        self.players = state.players
        self.league_path = Path(league_path)
        self.managers = league_managers(league_path)
        self._tendencies: tuple[int, list] = (-1, [])
        self.tracker = RoomTracker(self.league, self.players, picks_path, log=self._log)
        self.raw_log = Path(raw_log) if raw_log else None
        self.sims, self.candidates = sims, candidates
        self.lock = threading.RLock()
        self.wake = threading.Condition(self.lock)
        self.result: dict = {"version": -1}
        self.computing = False
        self.predictions: list[dict] = []
        self.pred_history: dict[int, dict[str, float]] = {}  # pick -> {player: P} predicted beforehand
        self.page_connected: float | None = None
        threading.Thread(target=self._worker, daemon=True).start()

    def _log(self, line: str) -> None:
        print(line, flush=True)

    # --- state -----------------------------------------------------------------

    def state(self) -> DraftState:
        st = DraftState(self.league, self.players)
        apply_log(st, self.tracker.entries)
        return st

    def _changed(self) -> None:
        with self.wake:
            self.wake.notify_all()

    def _worker(self) -> None:
        while True:
            with self.wake:
                while self.result["version"] == self.tracker.version:
                    self.wake.wait(timeout=5)
                version = self.tracker.version
                st = self.state()
                self.computing = True
            try:
                result = self._compute(st)
            except Exception:
                traceback.print_exc()
                result = {"error": "recommendation failed — see terminal"}
            with self.lock:
                self.computing = False
                result["version"] = version
                self.result = result
            self._predict(st)

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
                self.pred_history[row["pick"]] = dict(list(row["probs"].items())[:15])

    def _compute(self, st: DraftState) -> dict:
        if st.is_complete or not st.my_next_picks():
            return {"recs": [], "available": []}
        recs = recommend(st, candidates=self.candidates, sims=self.sims)
        probs = availability(st, sims=max(self.sims * 2, 100))
        advice = self._advice(st, recs, probs)
        top = sorted((p for p in st.available() if not is_placeholder(p)),
                     key=lambda p: p.proj_points, reverse=True)[:40]
        best = recs[0].exp_final_value if recs else 0.0
        return {
            "advice": advice,
            "recs": [{
                "id": r.player.id, "name": r.player.name, "pos": _pos(r.player),
                "team": r.player.nba_team, "proj": round(r.player.proj_points),
                "rank": _num(r.player.espn_rank), "gain": round(r.marginal_now),
                "vs_espn": round(r.player.proj_points - r.player.room_points),
                "sources": [[n, round(v)] for n, v in r.player.sources], "agree": _agree(r.player),
                "p_now": r.p_avail_now, "p_after": r.p_avail_after,
                "value": round(r.exp_final_value), "vs_best": round(r.exp_final_value - best),
            } for r in recs],
            "available": [{
                "id": p.id, "name": p.name, "pos": _pos(p), "team": p.nba_team,
                "proj": round(p.proj_points), "rank": _num(p.espn_rank), "adp": _num(p.adp),
                "vs_espn": round(p.proj_points - p.room_points), "spread": round(p.spread),
                "sources": [[n, round(v)] for n, v in p.sources], "agree": _agree(p),
                "p": probs.get(p.id, [])[:3],
            } for p in top],
            "underrated": self._underrated(st, probs),
            "has_sources": any(len(p.sources) > 1 for p in st.players.values()),
        }

    @staticmethod
    def _advice(st: DraftState, recs, probs: dict | None = None) -> dict | None:
        """Plain-language note on whether to take the highest-projected player."""
        if not recs:
            return None
        top = recs[0]
        best = max(recs, key=lambda r: r.player.proj_points)
        mine = st.my_next_picks()
        on_clock = mine[0] == st.current_pick
        now_pick = mine[0] + 1
        later = mine[1] + 1 if len(mine) > 1 else None
        if best.player.id == top.player.id:
            return {"kind": "same", "top": top.player.name, "pick": now_pick}
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
                "best": best.player.name, "best_proj": round(best.player.proj_points),
                "top_proj": round(top.player.proj_points), "gain": gain, "reasons": reasons,
                "pick": now_pick}

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
        with self.lock:
            tr = self.tracker
            st = self.state()
            lg = self.league
            pick = st.current_pick
            mine = st.my_next_picks()
            rosters = st.rosters()
            owners, bench = assign_lineup(rosters[lg.my_slot], lg.starting_slots)
            on_clock = lg.team_for_pick(pick) if not st.is_complete else None
            recent = []
            for i in range(max(0, pick - 30), pick):
                p = self.players[st.picks[i]]
                slot = lg.team_for_pick(i)
                recent.append({"pick": i + 1, "team": tr.name(slot), "slot": slot, "name": p.name,
                               "pos": _pos(p), "auto": i in st.auto_picks, "mine": slot == lg.my_slot})
            suggestion = self.players.get(tr.suggestion) if tr.suggestion else None
            filled, over = target_fill(rosters[lg.my_slot], lg)
            tendencies = self._team_tendencies(st)
            team_counts = []
            for roster in rosters:
                counts: dict[str, float] = {}
                for p in roster:
                    add_to_counts(counts, p)
                team_counts.append(counts)
            return {
                "version": tr.version,
                "computing": self.computing or self.result["version"] != tr.version,
                "status": {
                    "pick": pick + 1, "round": pick // lg.num_teams + 1, "rounds": lg.rounds,
                    "teams": lg.num_teams, "complete": st.is_complete,
                    "on_clock": None if on_clock is None else {"slot": on_clock, "name": tr.name(on_clock)},
                    "my_next": mine[0] + 1 if mine else None,
                    "picks_until": mine[0] - pick if mine else None,
                    "my_picks": [m + 1 for m in mine],
                    "seconds_left": tr.seconds_left(),
                },
                "feed": {
                    "connected": self.page_connected is not None,
                    "last_message_age": None if tr.last_feed is None else time.time() - tr.last_feed,
                    "blocked": tr.blocked,
                },
                "suggestion": None if suggestion is None else {"id": suggestion.id, "name": suggestion.name,
                                                               "pos": _pos(suggestion)},
                "me": {"slot": lg.my_slot, "name": tr.name(lg.my_slot)},
                "lineup": [{"slot": s, "name": p.name if p else None, "pos": _pos(p) if p else "",
                            "proj": round(p.proj_points) if p else None}
                           for s, p in zip(lg.starting_slots, owners)],
                "bench": [{"name": p.name, "pos": _pos(p), "proj": round(p.proj_points)} for p in bench],
                "bench_slots": lg.roster_slots.get("BE", 0),
                "teams": [{
                    "slot": i, "name": tr.name(i), "strategy": t.strategy, "noise": t.noise,
                    "manager": t.manager, "view": t.view, "favorite_team": t.favorite_team,
                    "auto": i in st.auto_teams, "me": i == lg.my_slot, "count": len(rosters[i]),
                    "last": rosters[i][-1].name if rosters[i] else None,
                    "observed": tendencies[i],
                    "modelled_as": (tendencies[i] or {}).get("used") or
                                   ("adp" if t.strategy == "unknown" else t.strategy),
                    "full": sorted(full_positions(team_counts[i], lg.targets)),
                } for i, t in enumerate(lg.teams)],
                "positions": {"targets": lg.targets, "filled": filled,
                              "over": [p.name for p in over]},
                "recent": recent[::-1],
                "strip": self._strip(st),
                "log": list(tr.recent_log)[-12:],
                "result": self.result,
            }

    def _strip(self, st: DraftState) -> dict:
        """Pick strip: recent picks scored against what was predicted, then the
        pick on the clock and upcoming picks with their most likely players."""
        lg, tr = self.league, self.tracker
        pick = st.current_pick

        def brief(pid: str) -> dict:
            p = self.players[pid]
            return {"name": p.name, "pos": _pos(p)}

        past = []
        for i in range(max(0, pick - 4), pick):
            pid, slot = st.picks[i], lg.team_for_pick(i)
            pred = self.pred_history.get(i)
            guess = next(iter(pred.items()), None) if pred else None
            past.append({"pick": i + 1, "team": tr.name(slot), "mine": slot == lg.my_slot,
                         "auto": i in st.auto_picks, **brief(pid),
                         "p": None if pred is None or slot == lg.my_slot else pred.get(pid, 0.0),
                         "guess": None if guess is None else {**brief(guess[0]), "p": guess[1]}})
        upcoming = []
        top_rec = (self.result.get("recs") or [None])[0]
        for row in self.predictions:
            if row["pick"] < pick:
                continue
            slot = row["team"]
            upcoming.append({
                "pick": row["pick"] + 1, "team": tr.name(slot), "mine": slot == lg.my_slot,
                "auto": slot in st.auto_teams, "now": row["pick"] == pick,
                "likely": [{**brief(pid), "p": p} for pid, p in list(row["probs"].items())[:3]],
                "rec": top_rec["name"] if slot == lg.my_slot and top_rec and row["pick"] == st.my_next_picks()[0] else None,
            })
        # How well the model has predicted opponents' picks so far.
        scored = [(self.pred_history[i], pid) for i, pid in enumerate(st.picks)
                  if i in self.pred_history and lg.team_for_pick(i) != lg.my_slot]
        accuracy = None
        if scored:
            accuracy = {
                "picks": len(scored),
                "top_hit": sum(next(iter(pr)) == pid for pr, pid in scored if pr) / len(scored),
                "avg_p": sum(pr.get(pid, 0.0) for pr, pid in scored) / len(scored),
            }
        return {"past": past, "upcoming": upcoming, "accuracy": accuracy}

    def _team_tendencies(self, st: DraftState) -> list[dict | None]:
        """What each team's own (non-autodraft) picks so far suggest; cached per draft version."""
        version = self.tracker.version
        if self._tendencies[0] != version:
            fits = fit_teams(st) if st.picks else {}
            rows = []
            for i, t in enumerate(analyze(st)):
                f = fits.get(i)
                if t.picks == 0:
                    rows.append(None)
                    continue
                best = max(f.fit, key=f.fit.get) if f else None
                # A team that always takes the top ESPN rank looks like autodraft.
                suggested = "auto" if t.picks >= 3 and t.top_rank_rate >= 0.9 else \
                    best if f and f.fit[best] >= 0.6 else None
                rows.append({
                    "picks": t.picks, "auto_picks": t.auto_picks,
                    "top_rank": round(t.top_rank_rate, 2), "vs_adp": round(t.reach_vs_adp, 1),
                    "fit": {k: round(v, 2) for k, v in f.fit.items()} if f else None,
                    "best": best, "best_p": round(f.fit[best], 2) if f else None,
                    "noise": f.noise if f else None, "prior_noise": f.prior_noise if f else None,
                    "used": f.strategy if f else None,
                    "suggested": suggested,
                })
            self._tendencies = (version, rows)
        return self._tendencies[1]

    def set_strategy(self, slot: int, strategy: str) -> str:
        """Change a team's saved strategy, here and in the league file."""
        lg = self.league
        if not 0 <= slot < lg.num_teams:
            raise ValueError("no such team")
        if strategy not in TeamProfile.VALID_STRATEGIES:
            raise ValueError(f"unknown strategy {strategy!r}")
        lg.teams[slot] = replace(lg.teams[slot], strategy=strategy)
        cfg = json.loads(self.league_path.read_text(encoding="utf-8"))
        cfg["teams"][slot]["strategy"] = strategy
        self.league_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        self.tracker.version += 1  # re-run recommendations with the new model
        name = self.tracker.name(slot)
        self.tracker.log(f"   {name}: strategy set to {strategy}")
        return f"{name} now modelled as '{strategy}'"

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
                msg = tr.manual_pick(str(body["id"]))
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
        def _send(self, code: int, body: bytes = b"", ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Access-Control-Allow-Origin", "*")
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
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._json({"ok": False, "error": "bad JSON"}, 400)
                return
            path = self.path.split("?")[0]
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
