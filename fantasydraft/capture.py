"""Local receiver for the ESPN draft-room capture userscript.

The userscript (browser/espn_draft_capture.user.js) forwards what the ESPN
draft page receives over its WebSocket, plus periodic snapshots of the page
text, to this server, which appends everything to a JSON-lines file. That
raw capture is what a reader for ESPN's live draft feed gets built from.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def serve(out: str | Path, port: int = 8765) -> None:
    out_path = Path(out)
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
            with out_path.open("a", encoding="utf-8") as f:
                for e in events:
                    e["received"] = now
                    f.write(json.dumps(e) + "\n")
                    counts[e.get("kind", "?")] += 1
                    kind = e.get("kind", "?")
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
    print(f"Capturing to {out_path} on http://127.0.0.1:{port} — open the ESPN draft page now. Ctrl+C to stop.")
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
    events = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
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

    urls = Counter(re.sub(r"\?.*", "", e.get("url", "")) for e in events if e.get("kind") in ("fetch", "ws-open"))
    if urls:
        out.append("\n== URLs ==")
        out += [f"  x{n}  {redact(u)}" for u, n in urls.most_common(20)]

    dom = [e for e in events if e.get("kind") == "dom-text"]
    if dom:
        out.append("\n== last page text (start) ==")
        out.append(redact(dom[-1].get("data", ""))[:3000])
    return "\n".join(out)
