"""Follow an ESPN draft room live, from the messages the browser userscript forwards.

ESPN's draft room feed (text over a WebSocket), as observed in a mock draft:

    SELECTING <team> <ms>                     team is on the clock
    SELECTED <team> <player> <slot> [<member>] pick made; <member> is only
                                              present when a person picked
                                              (absent for autodraft/timeouts)
    AUTODRAFT <team> true|false               team switched autodraft (a pick
                                              timeout switches it on)
    AUTOSUGGEST <player>                      ESPN's autopick for *you* now
    TOKEN a:b:<your team>:...                 your ESPN team id

ESPN team ids are not draft positions, so the mapping is learned from which
team is on the clock at each pick. Picks and autodraft switches are written
to the picks file (as @auto/@back lines), which `watch` and `draft` read.
"""
from __future__ import annotations

import json
import re
import time
from collections import deque
from pathlib import Path
from typing import Callable

from .io import _norm, load_log, placeholder_player, save_log
from .models import League, Player

PICK_LINE = re.compile(r"^\s*([^\n/]+?)\s*/\s*[A-Z]{2,4}\b[^\n]*\n\s*R(\d+),\s*P(\d+)\s*-\s*([^\n]+)$", re.M)
ON_CLOCK = re.compile(r"ON THE CLOCK:\s*PICK\s+(\d+)", re.I)
# The pick carousel marks each new round: "ROUND | 2 | PICK 15" means 14 teams.
ROUND_START = re.compile(r"\bROUND\W+(\d+)\W+PICK\s+(\d+)", re.I)
WAITING = "waiting for the draft page to confirm the pick count"


class RoomTracker:
    def __init__(self, league: League, players: dict[str, Player], picks_path: str | Path,
                 log: Callable[[str], None] = print):
        self.league = league
        self.players = players
        self.path = Path(picks_path)
        self.log = log
        self.entries: list[tuple] = []
        unreadable = None
        if self.path.exists():
            try:
                self.entries = load_log(self.path, players, league)
            except ValueError as e:  # e.g. a picks file from a draft with more teams
                unreadable = str(e)
        self.team_slot: dict[int, int] = {}  # ESPN team id -> 0-based draft slot
        self.pending_auto: dict[int, bool] = {}  # switches seen before a team was mapped
        self.my_team_id: int | None = None
        self.blocked: str | None = None  # set while our pick count disagrees with the page
        self.buffer: list[str] = []  # feed messages held while blocked
        self.suggestion: str | None = None
        self.slot_names: dict[int, str] = {}  # learned from page text, display only
        self.version = 0  # bumped whenever picks/autodraft change
        self.clock: tuple[float, float] | None = None  # (when seen, seconds left then)
        self.last_feed: float | None = None  # time of the last draft-room message
        self.recent_log: deque[str] = deque(maxlen=60)
        self.page_done: int | None = None  # picks made so far, per the draft page
        self._replaying = False  # replaying held messages: their clock values are stale
        self.resumed = bool(self.entries)
        # The draft's real number of teams, when it turns out to differ from the
        # league file (negative: "at least this many"); and who picked last.
        self.team_count: int | None = None
        self.last_team: int | None = None
        self.hold_since: float | None = None  # when we started waiting for the page
        self.trust_feed = False  # the page never showed a pick number: go by the feed alone  # restarted mid-draft: picks may have been missed meanwhile
        # The ESPN team id -> slot mapping is kept next to the picks file, so a
        # restart mid-draft doesn't have to relearn it from whoever is on the clock.
        self.map_path = self.path.with_name(self.path.name + ".teams.json")
        if self.entries and self.map_path.exists():
            try:
                saved = json.loads(self.map_path.read_text(encoding="utf-8"))
                if saved.get("first_pick") == self.picks[0]:  # same draft, not a reused file name
                    self.team_slot = {int(k): int(v) for k, v in saved["teams"].items()}
            except (ValueError, OSError, AttributeError, KeyError, IndexError, TypeError):
                self.team_slot = {}
        outer_log = log

        def _log(line: str) -> None:
            self.recent_log.append(line)
            outer_log(line)

        self.log = _log
        if unreadable:
            self._start_fresh(f"{self.path.name} doesn't fit this league ({unreadable}), "
                              "so it's from another draft")
        if self.entries:
            self.log(f"resuming with {len(self.picks)} picks already in {self.path}")

    # --- derived state ---------------------------------------------------------

    @property
    def picks(self) -> list[str]:
        return [e[1] for e in self.entries if e[0] == "pick"]

    def auto_slots(self) -> set[int]:
        on: set[int] = set(self.league.start_auto)
        for e in self.entries:
            if e[0] == "auto":
                (on.add if e[2] else on.discard)(e[1])
        return on

    def name(self, slot: int) -> str:
        return self.slot_names.get(slot) or self.league.teams[slot].name

    def _save(self) -> None:
        save_log(self.entries, self.players, self.league, self.path)
        self.version += 1

    def seconds_left(self) -> float | None:
        if self.clock is None:
            return None
        seen, left = self.clock
        return max(left - (time.time() - seen), 0.0)

    # --- manual control (UI) ------------------------------------------------------

    def manual_pick(self, pid: str | None = None, name: str | None = None) -> str:
        """Record the next pick by hand. `name` records someone missing from
        your players list (kept as a placeholder so later picks stay aligned)."""
        if pid is None and name:
            pid = self._unlisted(name)
        if pid not in self.players:
            raise ValueError(f"unknown player {pid}")
        if pid in self.picks:
            raise ValueError(f"{self.players[pid].name} is already drafted")
        pick = len(self.picks)
        if pick >= self.league.total_picks:
            raise ValueError("the draft is complete")
        self.entries.append(("pick", pid))
        self._save()
        slot = self.league.team_for_pick(pick)
        self.clock = None
        line = f"#{pick + 1:>3} {self.name(slot):<24} {self.players[pid].name}  (entered by hand)"
        self.log(line)
        if self.blocked:
            self.try_release()
        return line

    def _unlisted(self, name: str) -> str:
        pid = "unlisted-" + (_norm(name).replace(" ", "-") or "player")
        self.players.setdefault(pid, placeholder_player(pid))
        return pid

    def missing(self) -> tuple[int, int | None] | None:
        """While out of sync: (first missing pick number, how many are missing or None if unknown)."""
        if not self.blocked:
            return None
        first = len(self.picks) + 1
        if self.page_done is None:
            return first, None
        held = sum(1 for m in self.buffer if m.startswith("SELECTED"))
        return first, max(self.page_done - len(self.picks) - held, 0)

    def try_release(self) -> None:
        """Replay held feed messages now that missing picks may have been filled in
        by hand; if they still don't line up, we stay held."""
        held = sum(1 for m in self.buffer if m.startswith("SELECTED"))
        if self.page_done is not None and len(self.picks) + held < self.page_done:
            return  # still missing picks: replaying now would put held picks on the wrong teams
        pending, self.buffer = self.buffer, []
        self.blocked = None
        self._replay(pending)
        if not self.blocked:
            self.log("Back in sync.")

    def _replay(self, messages: list[str]) -> None:
        self._replaying = True
        try:
            for data in messages:
                self.handle_message(data)
        finally:
            self._replaying = False

    def undo(self) -> str:
        if not self.entries:
            raise ValueError("nothing to undo")
        last = self.entries.pop()
        self._save()
        what = self.players[last[1]].name if last[0] == "pick" else \
            f"{self.name(last[1])} {'auto' if last[2] else 'back'}"
        self.log(f"   undid: {what}")
        if self.blocked:
            self.try_release()
        return what

    def manual_auto(self, slot: int, on: bool) -> None:
        if not 0 <= slot < self.league.num_teams:
            raise ValueError("no such team")
        if self._set_auto(slot, on):
            self._save()

    # --- team mapping --------------------------------------------------------------

    def _map(self, team_id: int, pick: int) -> bool:
        """Learn/verify team_id from it being on the clock at `pick`. False if inconsistent."""
        if pick >= self.league.total_picks:
            return True
        slot = self.league.team_for_pick(pick)
        known = self.team_slot.get(team_id)
        if known is None:
            if slot in self.team_slot.values():
                return False
            self.team_slot[team_id] = slot
            self._save_map()
            if team_id == self.my_team_id and slot != self.league.my_slot:
                self.log(f"!! ESPN says you draft #{slot + 1}, but your league file says "
                         f"#{self.league.my_slot + 1}. Recreate it with new-league --my-pick {slot + 1}.")
            if team_id in self.pending_auto and self._set_auto(slot, self.pending_auto.pop(team_id)):
                self._save()
            return True
        return known == slot

    def _save_map(self) -> None:
        try:
            first = self.picks[0] if self.picks else None
            self.map_path.write_text(json.dumps({"first_pick": first, "teams": self.team_slot}), encoding="utf-8")
        except OSError:
            pass  # only a convenience for restarts

    def _set_auto(self, slot: int, on: bool) -> bool:
        if on == (slot in self.auto_slots()):
            return False
        self.entries.append(("auto", slot, on))
        self.log(f"   {self.name(slot)} {'is ON autodraft' if on else 'is back from autodraft'}")
        return True

    def _start_fresh(self, why: str) -> None:
        backup = self.path.with_name(f"{self.path.stem}.old-{time.strftime('%Y%m%d-%H%M%S')}{self.path.suffix}")
        try:
            self.path.replace(backup)
            self.map_path.unlink(missing_ok=True)
            kept = f"; the old picks are saved as {backup.name}"
        except OSError:
            kept = ""
        self.log(f"!! Starting fresh: {why}{kept}.")
        pending = self.buffer  # messages held meanwhile belong to the new draft
        self.entries, self.buffer, self.blocked = [], [], None
        self.team_slot, self.pending_auto = {}, {}
        self.resumed, self.team_count, self.last_team = False, None, None
        self._save()
        self._replay(pending)

    def _team_count_text(self) -> str:
        c = self.team_count
        return "?" if c is None else str(c) if c > 0 else f"at least {-c}"

    def my_draft_position(self) -> int | None:
        """Your draft position (1-based) as the draft room showed it, if known."""
        slot = self.team_slot.get(self.my_team_id) if self.my_team_id is not None else None
        return None if slot is None else slot + 1

    def _out_of_sync(self, why: str) -> None:
        if not self.blocked:
            self.log(f"!! Lost track of the draft ({why}). Holding new picks until it resyncs "
                     "from the page. Open the draft room's Pick History so it can be read.")
            n = self.league.num_teams
            if len(self.picks) < 2 * n:
                # Early in the draft, the usual cause is the snake turning at the wrong place.
                self.log(f"!! This early, the likely cause is the team count: your league file has {n} teams. "
                         "If the draft has a different number, recreate the file "
                         "(new-league --teams N --my-pick P --force), then restart room.")
        self.blocked = why

    # --- feed ------------------------------------------------------------------

    def handle_message(self, data: str) -> None:
        parts = str(data).split()
        if not parts:
            return
        kind = parts[0]
        if not self._replaying:
            self.last_feed = time.time()
            try:  # the countdown keeps running even while picks are held
                if kind in ("CLOCK", "SELECTING") and len(parts) >= 3:
                    self.clock = (time.time(), int(parts[2]) / 1000)
            except ValueError:
                pass
        if self.blocked == WAITING and not self._replaying and self.hold_since \
                and time.time() - self.hold_since > 20:
            # The page never showed "ON THE CLOCK: PICK n": go by the feed alone.
            self.log("   (the draft page didn't show a pick number; going by the feed alone)")
            self.trust_feed = True
            self.buffer.append(data)
            self.try_release()
            return
        if self.blocked:
            self.buffer.append(data)
            return
        try:
            if kind == "TOKEN" and len(parts) > 1:
                fields = parts[1].split(":")
                if len(fields) > 2 and fields[2].isdigit():
                    self.my_team_id = int(fields[2])
            elif kind in ("SELECTING", "SELECTED") and len(parts) >= 2 and self._unconfirmed(int(parts[1])):
                # Started late or restarted: a team we haven't mapped yet would be
                # mapped to whatever pick we think is next; wait for the page's pick count.
                self.blocked = WAITING
                self.hold_since = time.time()
                self.buffer.append(data)
            elif kind == "SELECTING" and len(parts) >= 2:
                team_id = int(parts[1])
                # A known team on the clock "at the wrong pick" is usually just a
                # late or repeated message; only the picks themselves decide sync.
                if team_id not in self.team_slot and not self._map(team_id, len(self.picks)):
                    self._out_of_sync(f"team {parts[1]} on the clock at pick {len(self.picks) + 1}")
            elif kind == "AUTODRAFT" and len(parts) >= 3:
                team_id, on = int(parts[1]), parts[2].lower() == "true"
                if team_id in self.team_slot:
                    if self._set_auto(self.team_slot[team_id], on):
                        self._save()
                else:
                    self.pending_auto[team_id] = on
            elif kind == "SELECTED" and len(parts) >= 3:
                self._selected(int(parts[1]), parts[2], by_person=len(parts) >= 5)
            elif kind == "AUTOSUGGEST" and len(parts) >= 2:
                self.suggestion = parts[1]
        except ValueError:
            pass  # not the format we know; ignore rather than crash mid-draft

    def _unconfirmed(self, team_id: int) -> bool:
        return self.page_done is None and not self.trust_feed and team_id not in self.team_slot

    def _selected(self, team_id: int, pid: str, by_person: bool) -> None:
        if pid in self.picks:
            return  # replay of a pick we already have
        pick = len(self.picks)
        if pick >= self.league.total_picks:
            self.log(f"!! more picks than the league file allows ({self.league.total_picks}); check its size")
            return
        if not self._map(team_id, pick):
            self._out_of_sync(f"team {team_id} picked at pick {pick + 1}")
            self.buffer.append(f"SELECTED {team_id} {pid} 0" + (" x" if by_person else ""))
            return
        self.last_team = team_id
        slot = self.team_slot[team_id]
        # The pick itself tells us whether a person or autodraft made it.
        self._set_auto(slot, not by_person)
        if pid not in self.players:
            self.players[pid] = placeholder_player(pid)
            self.log(f"   (player {pid} isn't in your players list; recorded as unknown)")
        self.entries.append(("pick", pid))
        self._save()
        if len(self.picks) == 1:
            self._save_map()
        tag = "" if by_person else "  [auto]"
        mine = "  <- YOU" if slot == self.league.my_slot else ""
        self.log(f"#{pick + 1:>3} {self.name(slot):<24} {self.players[pid].name}{tag}{mine}")

    # --- page text: resync after joining mid-draft --------------------------------

    def handle_page_text(self, text: str) -> None:
        # The carousel's round markers give the real number of teams:
        # round r starts at pick (r - 1) * teams + 1.
        sizes = {(int(pk) - 1) // (int(rd) - 1) for rd, pk in ROUND_START.findall(text)
                 if int(rd) >= 2 and (int(pk) - 1) % (int(rd) - 1) == 0}
        if len(sizes) == 1:
            n = sizes.pop()
            real = None if n == self.league.num_teams else n
            if real != self.team_count:
                self.team_count = real
                if real:
                    self.log(f"!! This draft has {real} teams, but your league file has {self.league.num_teams}.")
        for name, rnd, pos, team in PICK_LINE.findall(text):
            n = self.league.num_teams
            r, p = int(rnd), int(pos)
            if 1 <= p <= n:
                self.slot_names.setdefault(p - 1 if r % 2 else n - p, team.strip())
        m = ON_CLOCK.search(text)
        if not m:
            return
        done = int(m.group(1)) - 1
        self.page_done = done
        if done < len(self.picks):
            # Picks can't un-happen: the file is from an earlier draft.
            self._start_fresh(f"the draft page is at pick {done + 1} but {self.path.name} already has "
                              f"{len(self.picks)} picks (from an earlier draft)")
        if len(self.picks) >= done and not self.blocked:
            return
        if self.blocked and len(self.picks) + sum(m.startswith("SELECTED") for m in self.buffer) >= done:
            self.try_release()  # e.g. the missing picks were entered by hand
            if not self.blocked:
                return
        recovered = self._recover(text, done)
        if recovered is None:
            self._out_of_sync(f"page is at pick {done + 1}, but only {len(self.picks)} picks are known")
            return
        was_auto = self.auto_slots()
        self.entries = [("pick", pid) for pid in recovered]
        # Keep who is on autodraft (ESPN only says so again when it changes).
        start = set(self.league.start_auto)
        self.entries += [("auto", slot, slot in was_auto) for slot in sorted(was_auto ^ start)]
        self.blocked = None
        self.log(f"Resynced from the page: {len(recovered)} picks.")
        self._save()
        # Held messages about picks the page already gave us are stale: replay
        # only what came after the last of them.
        pending, self.buffer = self.buffer, []
        got = set(recovered)
        last = max((i for i, m in enumerate(pending)
                    if m.startswith("SELECTED") and len(m.split()) > 2 and m.split()[2] in got), default=-1)
        self._replay(pending[last + 1:])

    def _recover(self, text: str, done: int) -> list[str] | None:
        """Picks 1..done from the picks we have plus the page's pick list, or None
        if together they don't cover them all (or disagree)."""
        index: dict[str, str] = {}
        for pid, p in self.players.items():
            index.setdefault(_norm(p.name), pid)
        by_pick: dict[int, str] = {}
        known = self.picks
        for name, rnd, pos, _team in PICK_LINE.findall(text):
            k = (int(rnd) - 1) * self.league.num_teams + int(pos)
            pid = index.get(_norm(name))
            if pid is None:  # not in your players list: keep what we had, else a placeholder
                pid = known[k - 1] if k - 1 < len(known) else self._unlisted(name)
            by_pick[k] = pid
        # ESPN's pick list may show only recent picks: combine it with the picks
        # we already have, as long as the two agree wherever they overlap.
        if any(k <= len(known) and by_pick[k] != known[k - 1] for k in by_pick if not
               self.players[by_pick[k]].name.startswith("Unknown player")):
            return None
        merged = {k: known[k - 1] for k in range(1, len(known) + 1)}
        merged.update({k: v for k, v in by_pick.items() if k > len(known)})
        if not all(k in merged for k in range(1, done + 1)):
            return None
        return [merged[k] for k in range(1, done + 1)]
