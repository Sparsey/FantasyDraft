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

import re
import time
from collections import deque
from pathlib import Path
from typing import Callable

from .io import _norm, load_log, placeholder_player, save_log
from .models import League, Player

PICK_LINE = re.compile(r"^\s*([^\n/]+?)\s*/\s*[A-Z]{2,4}\b[^\n]*\n\s*R(\d+),\s*P(\d+)\s*-\s*([^\n]+)$", re.M)
ON_CLOCK = re.compile(r"ON THE CLOCK:\s*PICK\s+(\d+)", re.I)


class RoomTracker:
    def __init__(self, league: League, players: dict[str, Player], picks_path: str | Path,
                 log: Callable[[str], None] = print):
        self.league = league
        self.players = players
        self.path = Path(picks_path)
        self.log = log
        self.entries: list[tuple] = load_log(self.path, players, league) if self.path.exists() else []
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
        outer_log = log

        def _log(line: str) -> None:
            self.recent_log.append(line)
            outer_log(line)

        self.log = _log
        if self.entries:
            self.log(f"resuming with {len(self.picks)} picks already in {self.path}")

    # --- derived state ---------------------------------------------------------

    @property
    def picks(self) -> list[str]:
        return [e[1] for e in self.entries if e[0] == "pick"]

    def auto_slots(self) -> set[int]:
        on: set[int] = set()
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

    def manual_pick(self, pid: str) -> str:
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
        return line

    def undo(self) -> str:
        if not self.entries:
            raise ValueError("nothing to undo")
        last = self.entries.pop()
        self._save()
        what = self.players[last[1]].name if last[0] == "pick" else \
            f"{self.name(last[1])} {'auto' if last[2] else 'back'}"
        self.log(f"   undid: {what}")
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
            if team_id == self.my_team_id and slot != self.league.my_slot:
                self.log(f"!! ESPN says you draft #{slot + 1}, but your league file says "
                         f"#{self.league.my_slot + 1}. Recreate it with new-league --my-pick {slot + 1}.")
            if team_id in self.pending_auto:
                self._set_auto(slot, self.pending_auto.pop(team_id))
            return True
        return known == slot

    def _set_auto(self, slot: int, on: bool) -> bool:
        if on == (slot in self.auto_slots()):
            return False
        self.entries.append(("auto", slot, on))
        self.log(f"   {self.name(slot)} {'is ON autodraft' if on else 'is back from autodraft'}")
        return True

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
        if self.blocked:
            self.buffer.append(data)
            return
        parts = str(data).split()
        if not parts:
            return
        kind = parts[0]
        self.last_feed = time.time()
        try:
            if kind == "CLOCK" and len(parts) >= 3:
                self.clock = (time.time(), int(parts[2]) / 1000)
            elif kind == "SELECTING" and len(parts) >= 3:
                self.clock = (time.time(), int(parts[2]) / 1000)
            if kind == "TOKEN" and len(parts) > 1:
                fields = parts[1].split(":")
                if len(fields) > 2 and fields[2].isdigit():
                    self.my_team_id = int(fields[2])
            elif kind == "SELECTING" and len(parts) >= 2:
                if not self._map(int(parts[1]), len(self.picks)):
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
        slot = self.team_slot[team_id]
        # The pick itself tells us whether a person or autodraft made it.
        self._set_auto(slot, not by_person)
        if pid not in self.players:
            self.players[pid] = placeholder_player(pid)
            self.log(f"   (player {pid} isn't in your players list; recorded as unknown)")
        self.entries.append(("pick", pid))
        self._save()
        tag = "" if by_person else "  [auto]"
        mine = "  <- YOU" if slot == self.league.my_slot else ""
        self.log(f"#{pick + 1:>3} {self.name(slot):<24} {self.players[pid].name}{tag}{mine}")

    # --- page text: resync after joining mid-draft --------------------------------

    def handle_page_text(self, text: str) -> None:
        for name, rnd, pos, team in PICK_LINE.findall(text):
            n = self.league.num_teams
            r, p = int(rnd), int(pos)
            if 1 <= p <= n:
                self.slot_names.setdefault(p - 1 if r % 2 else n - p, team.strip())
        m = ON_CLOCK.search(text)
        if not m:
            return
        done = int(m.group(1)) - 1
        if len(self.picks) >= done and not self.blocked:
            return
        recovered = self._recover(text, done)
        if recovered is None:
            self._out_of_sync(f"page is at pick {done + 1}, but only {len(self.picks)} picks are known")
            return
        self.entries = [("pick", pid) for pid in recovered]
        self.team_slot.clear()
        self.blocked = None
        self.log(f"Resynced from the page: {len(recovered)} picks.")
        self._save()
        pending, self.buffer = self.buffer, []
        for data in pending:
            self.handle_message(data)

    def _recover(self, text: str, done: int) -> list[str] | None:
        """Picks 1..done from the page's pick list, or None if it isn't all there."""
        index: dict[str, str] = {}
        for pid, p in self.players.items():
            index.setdefault(_norm(p.name), pid)
        by_pick: dict[int, str] = {}
        for name, rnd, pos, _team in PICK_LINE.findall(text):
            pid = index.get(_norm(name))
            if pid is None:
                return None
            by_pick[(int(rnd) - 1) * self.league.num_teams + int(pos)] = pid
        if not all(k in by_pick for k in range(1, done + 1)):
            return None
        return [by_pick[k] for k in range(1, done + 1)]
