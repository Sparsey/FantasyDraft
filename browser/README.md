# Reading picks straight from the ESPN draft page

**Status: capture only, for now.** The draft page receives picks live over a
WebSocket, but ESPN doesn't document that feed. Step one is to record it
during one ESPN mock draft. The summary of that recording is what the
automatic reader gets built from. Until then, use `draft` and type ESPN
ranks.

## One-time setup

1. Install the **Tampermonkey** browser extension (Chrome, Edge or Firefox).
2. Open the Tampermonkey dashboard → **+** (new script), replace the template
   with all of [`espn_draft_capture.user.js`](espn_draft_capture.user.js), and save.
   - **Chrome:** Tampermonkey also needs **Allow user scripts** turned on
     (Extensions → Tampermonkey → Details), or **Developer mode** on the
     extensions page, depending on your Chrome version.
3. The script only does anything on espn.com pages with "draft" in the URL,
   and only sends data to your own computer (`127.0.0.1`).

## Capturing a mock draft

1. Start the receiver **before** opening the draft room:
   ```bash
   python -m fantasydraft capture --out capture.jsonl
   ```
2. Join an ESPN mock draft, and reload the page once it's open so the
   script is in place before the draft connects. The terminal should show
   `hello: ...` and then `ws ...` lines as picks happen.
   - If Tampermonkey asks whether the script may connect to `127.0.0.1`,
     choose **Always allow**.
   - If no `ws` lines appear, that's still useful: the page text is saved
     every 10 seconds anyway.
3. Let the draft run to the end. Toggling your own autodraft on and off once
   along the way helps show how ESPN signals it. Then press Ctrl+C.
4. Make the shareable summary:
   ```bash
   python -m fantasydraft capture --out capture.jsonl --summary capture_summary.txt
   ```
   Send me `capture_summary.txt` (paste it, or attach it).

**Don't share or commit `capture.jsonl` itself.** It can contain session
tokens. The summary redacts token-like strings, but skim it before sharing.
Both files are git-ignored.
