"""Local receiver for the ESPN draft-room capture userscript.

The userscript (browser/espn_draft_capture.user.js) forwards what the ESPN
draft page receives over its WebSocket, plus periodic snapshots of the page
text, to this server, which appends everything to a JSON-lines file. That
raw capture is what a reader for ESPN's live draft feed gets built from.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def serve(out: str | Path, port: int = 8765, on_event=None, quiet: bool = False) -> None:
    """Receive events from the userscript and append them to `out`.

    on_event: optional callback for each event (used by `room` to follow the draft).
    quiet: don't echo events to the terminal.
    """
    out_path = Path(out)
    lock = threading.Lock()
    counts: Counter[str] = Counter()

    class Handler(BaseHTTPRequestHandler):
        def _cors(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            body = json.dumps({"ok": True, "received": dict(counts)}).encode()
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            try:
                events = json.loads(self.rfile.read(length) or b"[]")
                if isinstance(events, dict):
                    events = [events]
            except json.JSONDecodeError:
                self.send_response(400)
                self._cors()
                self.end_headers()
                return
            now = time.time()
            with lock, out_path.open("a", encoding="utf-8") as f:
                for e in events:
                    e["received"] = now
                    f.write(json.dumps(e) + "\n")
                    counts[e.get("kind", "?")] += 1
                    kind = e.get("kind", "?")
                    if on_event is not None:
                        try:
                            on_event(e)
                        except Exception as err:  # never let a parsing bug drop the capture
                            print(f"!! error handling {kind}: {err}")
                    if quiet:
                        continue
                    if kind == "ws-in":
                        print(f"  ws  {str(e.get('data'))[:110]}")
                    elif kind in ("hello", "ws-open", "ws-close", "fetch"):
                        print(f"  {kind}: {str(e.get('url', ''))[:110]}")
                    elif kind == "dom-text":
                        print(f"  page text snapshot ({len(e.get('data', ''))} chars) — total received: {dict(counts)}")
            self.send_response(204)
            self._cors()
            self.end_headers()

        def log_message(self, *args) -> None:  # silence per-request logging
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Listening on http://127.0.0.1:{port} (raw log: {out_path}) — open the ESPN draft page now. Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print(f"\nStopped. Received: {dict(counts) or 'nothing'} -> {out_path}")


_TOKENISH = re.compile(r"[A-Za-z0-9%+/=_\-{}]{32,}")
_SECRET_FIELD = re.compile(r'("?(?:token|auth|cookie|s2|swid|session|key)[a-z_]*"?\s*[:=]\s*)("[^"]*"|\S+)', re.I)


def redact(text: str) -> str:
    text = _SECRET_FIELD.sub(r"\1<redacted>", text)
    return _TOKENISH.sub("<long-token>", text)


def _shape(data: str) -> str:
    """Group key for a message: its JSON keys, or its first word."""
    try:
        obj = json.loads(data)
        if isinstance(obj, dict):
            return "json{" + ",".join(sorted(obj)[:8]) + "}"
        return f"json-{type(obj).__name__}"
    except (json.JSONDecodeError, TypeError):
        return (str(data).split() or ["<empty>"])[0][:30]


def summarize(path: str | Path, samples: int = 3) -> str:
    """Redacted overview of a capture: message types with examples, safe to share."""
    events = [json.loads(line) for line in Path(path).read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    out = [f"{len(events)} events: {dict(Counter(e.get('kind', '?') for e in events))}"]

    for kind in ("ws-in", "ws-out"):
        groups: dict[str, list[str]] = {}
        for e in events:
            if e.get("kind") == kind:
                groups.setdefault(_shape(e.get("data", "")), []).append(str(e.get("data", "")))
        if groups:
            out.append(f"\n== {kind}: {len(groups)} message types ==")
            for shape, msgs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
                out.append(f"\n[{shape}] x{len(msgs)}")
                picks = msgs[:samples] if len(msgs) <= samples else [msgs[0], msgs[len(msgs) // 2], msgs[-1]]
                out += ["  " + redact(m)[:400] for m in picks]

    # Every incoming message in order, minus the high-volume clock/keep-alive
    # chatter, so the full pick sequence and autodraft switches are visible.
    noisy = ("CLOCK", "PONG", "PING")
    timeline = [e for e in events if e.get("kind") == "ws-in"
                and not str(e.get("data", "")).startswith(noisy)]
    if timeline:
        def when(e: dict) -> float:  # seconds; the browser's ms clock, else the receiver's
            return e["t"] / 1000 if isinstance(e.get("t"), (int, float)) else e.get("received", 0)

        start = when(timeline[0])
        out.append(f"\n== all incoming messages except {'/'.join(noisy)} ({len(timeline)}) ==")
        for e in timeline[:3000]:
            t = when(e) - start
            out.append(f"  +{t:7.0f}s  {redact(str(e.get('data', '')))[:300]}")
    clocks = [e for e in events if e.get("kind") == "ws-in" and str(e.get("data", "")).startswith("CLOCK")]
    if clocks:
        out.append("\n== CLOCK messages: first of each new (team, last number) combination ==")
        seen = set()
        for e in clocks:
            parts = str(e.get("data", "")).split()
            key = (parts[1:2], parts[3:4])
            if str(key) not in seen:
                seen.add(str(key))
                out.append("  " + redact(str(e.get("data", "")))[:120])

    # Web requests whose responses look like they carry draft rankings.
    rankish = []
    for e in events:
        if e.get("kind") in ("fetch", "xhr"):
            body = str(e.get("data", ""))
            types = sorted(set(re.findall(r'"(\w+)"\s*:\s*\{[^{}]*"rank"', body)))
            hits = body.count("draftRanksByRankType") + body.count('"rank"') + body.count('"draftRank')
            if hits:
                # Paths are safe to show once the query string (which can hold ids) is dropped.
                rankish.append((re.sub(r"\?.*", "", e.get("url", "")), len(body), hits, types,
                                re.findall(r'[?&]view=(\w+)', e.get("url", ""))))
    if rankish:
        out.append("\n== responses that mention rankings ==")
        for url, size, hits, types, views in rankish[:15]:
            out.append(f"  {url}  views={views or '-'}  {size:,} chars, {hits} rank fields"
                       + (f", rank lists: {', '.join(types)}" if types else ""))
    init = [e for e in events if e.get("kind") == "ws-in" and str(e.get("data", "")).startswith("INIT")]
    if init:
        out.append(f"\n== INIT message: {len(str(init[0].get('data', ''))):,} chars ==")

    urls = Counter(re.sub(r"\?.*", "", e.get("url", "")) for e in events if e.get("kind") in ("fetch", "xhr", "ws-open"))
    if urls:
        out.append("\n== URLs ==")
        out += [f"  x{n}  {redact(u)}" for u, n in urls.most_common(20)]

    dom = [e for e in events if e.get("kind") == "dom-text"]
    if dom:
        out.append("\n== last page text (start) ==")
        out.append(redact(dom[-1].get("data", ""))[:3000])
    return "\n".join(out)


def split_sessions(events: list[dict], gap_s: float = 1800) -> list[list[dict]]:
    """Split a feed log (appended to across runs) into drafts: a new draft starts
    when the draft page's leagueId changes, or after a long quiet gap."""
    sessions: list[list[dict]] = []
    league, last_t = None, None
    for e in events:
        t = e.get("t") if isinstance(e.get("t"), (int, float)) else None
        new = not sessions
        if e.get("kind") == "hello":
            m = re.search(r"leagueId=(\d+)", str(e.get("url", "")))
            if m and league is not None and m.group(1) != league:
                new = True
            league = m.group(1) if m else league
        if t is not None and last_t is not None and (t - last_t) / 1000 > gap_s:
            new = True
        if new:
            sessions.append([])
        sessions[-1].append(e)
        last_t = t if t is not None else last_t
    return sessions


def replay_report(path: str | Path, league, players: dict, max_snapshots: int = 6,
                  session: int = -1) -> str:
    """Replay one draft from a dashboard/capture log through the live tracker and
    report, redacted, where it lost track, why, and what the page showed then."""
    import tempfile

    from .io import _norm
    from .room import ON_CLOCK, PICK_LINE, RoomTracker

    every = []
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        try:
            every.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # blank, or cut off when the dashboard was closed mid-write
    sessions = split_sessions(every)
    out = [f"{len(every)} events in {len(sessions)} draft(s):"]
    for i, ses in enumerate(sessions):
        ts = [e["t"] for e in ses if isinstance(e.get("t"), (int, float))]
        sel = sum(1 for e in ses if e.get("kind") == "ws-in" and str(e.get("data", "")).startswith("SELECTED"))
        when = time.strftime("%b %d %H:%M", time.localtime(ts[0] / 1000)) if ts else "?"
        out.append(f"  #{i + 1}: {when}, {len(ses)} events, {sel} picks seen")
    events = sessions[session]
    out.append(f"\nReplaying draft #{(session % len(sessions)) + 1} "
               f"(choose another with --session N)")
    names = {_norm(p.name) for p in players.values()}
    out += [f"{len(events)} events: {dict(Counter(e.get('kind', '?') for e in events))}",
            f"league file: {league.num_teams} teams, {league.rounds} rounds, you pick #{league.my_slot + 1}", ""]
    lines: list[str] = []
    t0 = next((e["t"] for e in events if isinstance(e.get("t"), (int, float))), 0)
    now = [0.0]
    with tempfile.TemporaryDirectory() as tmp:
        tr = RoomTracker(league, dict(players), Path(tmp) / "picks.txt",
                         log=lambda line: lines.append(f"+{now[0]:6.0f}s {line}"))
        shots = 0
        was_blocked = None
        repeats = 0
        for e in events:
            now[0] = (e.get("t", t0) - t0) / 1000 if isinstance(e.get("t"), (int, float)) else now[0]
            kind = e.get("kind")
            if kind == "hello":
                lines.append(f"+{now[0]:6.0f}s --- draft page (re)loaded ---")
            elif kind in ("ws-open", "ws-close"):
                lines.append(f"+{now[0]:6.0f}s --- feed connection {kind[3:]} ---")
            elif kind == "ws-in":
                tr.handle_message(str(e.get("data", "")))
            elif kind == "dom-text":
                text = str(e.get("data", ""))
                m = ON_CLOCK.search(text)
                found = PICK_LINE.findall(text)
                unknown = sorted({n.strip() for n, *_ in found if _norm(n) not in names})
                interesting = tr.blocked or (m and int(m.group(1)) - 1 != len(tr.picks))
                if interesting and shots < max_snapshots:
                    shots += 1
                    lines.append(f"+{now[0]:6.0f}s page: on the clock {m.group(1) if m else '?'}, "
                                 f"{len(tr.picks)} picks known, {len(found)} pick-list lines read"
                                 + (f", not in your list: {unknown[:8]}" if unknown else ""))
                    at = text.find("R1,") if "R1," in text else (m.start() if m else 0)
                    excerpt = redact(text[max(0, at - 300): at + 700]).replace("\n", " | ")
                    lines.append(f"         page text: {excerpt}")
                tr.handle_page_text(text)
            if tr.blocked != was_blocked:
                # the same reason with a new pick number: show the first few only
                same = bool(tr.blocked and was_blocked and tr.blocked.split()[:3] == was_blocked.split()[:3])
                repeats = repeats + 1 if same else 0
                if repeats < 3:
                    lines.append(f"+{now[0]:6.0f}s {'BLOCKED: ' + tr.blocked if tr.blocked else 'unblocked'}"
                                 f" (picks known {len(tr.picks)}, held {len(tr.buffer)})")
                elif repeats == 3:
                    lines.append("         (still blocked the same way; further lines left out)")
                was_blocked = tr.blocked
        out += lines
        out += ["", f"end: {len(tr.picks)} picks recorded, blocked: {tr.blocked}, "
                    f"team count seen: {tr.team_count or 'as in league file'}, "
                    f"your position per ESPN: {tr.my_draft_position()}"]
    return "\n".join(out)
