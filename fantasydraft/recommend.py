"""Pick recommendations for my next selection."""
from __future__ import annotations

import random
import zlib
from dataclasses import dataclass, field, replace
from statistics import mean

from .lineup import team_value
from .models import POSITIONS, DraftState, Player
from .simulate import Simulator, availability


def availability_sims(sims: int) -> int:
    return max(sims * 3, 200)


MIN_AVAIL = 0.05

def uncertainty(p: Player) -> float:
    """How unsure your projection of a player is: how much your sources
    disagree about him (0 when they agree, or with ESPN alone)."""
    return p.spread


def _drawn(p: Player, draw: int) -> Player:
    """Player p with points drawn for simulated "truth" number `draw`: the same
    player gets the same draw in every candidate's run of that simulated draft."""
    z = random.Random(zlib.crc32(f"{draw}:{p.id}".encode())).gauss(0.0, 1.0)
    return replace(p, proj_points=p.proj_points + z * uncertainty(p))  # below this chance of lasting to my pick, a player isn't shown as an option


@dataclass
class Recommendation:
    player: Player
    marginal_now: float  # immediate gain in my roster value
    p_avail_now: float  # P(still there at my next pick) — 1.0 if I'm on the clock
    p_avail_after: float  # P(still there at the pick after that) if I pass
    exp_final_value: float  # mean final roster value if I take them (or best fallback if gone)
    values: list[float] = field(default_factory=list)  # final roster value in each simulated draft (same seeds for all)
    # The same rosters scored on drawn "true" seasons (your projections can be
    # wrong): used for the chance of ending better, not for the average.
    drawn: list[float] = field(default_factory=list)


NOW_OR_NEVER = 3  # always compare this many good players who won't last to your following pick


def candidate_pool(state: DraftState, k: int, exclude: set[str] = frozenset(),
                   avail_probs: dict[str, list[float]] | None = None) -> list[Player]:
    """Top players by immediate roster gain, plus the best player at each
    position, plus the best few who probably won't last to your following pick
    (so "take him now, the other one later" is always among the plans compared)."""
    league = state.league
    roster = state.rosters()[league.my_slot]
    base = team_value(roster, league)
    avail = sorted((p for p in state.available() if p.id not in exclude),
                   key=lambda p: p.proj_points, reverse=True)[: k * 4]
    gains = {p.id: team_value(roster + [p], league) - base for p in avail}
    by_gain = sorted(avail, key=lambda p: (gains[p.id], p.proj_points), reverse=True)

    picked: dict[str, Player] = {p.id: p for p in by_gain[:k]}
    if avail:  # always include the highest-projected players you can get: by your numbers and ESPN's
        picked.setdefault(avail[0].id, avail[0])
        espn_top = max((p for p in state.available() if p.id not in exclude), key=lambda p: p.room_points)
        picked.setdefault(espn_top.id, espn_top)
    for pos in POSITIONS:
        best = next((p for p in by_gain if pos in p.positions), None)
        if best is not None:
            picked.setdefault(best.id, best)
    if avail_probs:
        soon_gone = [p for p in avail[: k * 2]
                     if len(avail_probs.get(p.id) or []) > 1 and avail_probs[p.id][1] < 0.5]
        for p in soon_gone[:NOW_OR_NEVER]:
            picked.setdefault(p.id, p)
    return list(picked.values())


def recommend(
    state: DraftState,
    candidates: int = 8,
    sims: int = 100,
    seed: int = 0,
    avail: dict[str, list[float]] | None = None,
    cancel=None,
) -> list[Recommendation]:
    """Rank candidates for my next pick by simulated end-of-draft roster value.

    The plan evaluated for each candidate is "take X at my next pick if X is
    still there, otherwise my default pick", so a great player I will almost
    never get scores like the fallback.

    Every candidate is evaluated on the same random seeds (common random
    numbers), so differences between them reflect the pick, not luck.

    cancel: called now and then; if it returns True the work is out of date
    (a new pick came in) and an empty list is returned at once.
    """
    if not state.my_next_picks():
        return []
    league = state.league
    roster = state.rosters()[league.my_slot]
    base = team_value(roster, league)
    my_picks = state.my_next_picks()
    on_clock = my_picks[0] == state.current_pick

    # Availability is cheap next to candidate evaluation; use enough runs that
    # "<1%" means something.
    if avail is None:
        avail = availability(state, sims=availability_sims(sims), seed=seed + 1)
    # Players who almost never last to my next pick aren't real options.
    gone = set() if on_clock else {pid for pid, pr in avail.items() if pr and pr[0] < MIN_AVAIL}
    sim = Simulator(state)
    out: list[Recommendation] = []
    for p in candidate_pool(state, candidates, exclude=gone, avail_probs=avail):
        values, drawn = [], []
        for i in range(sims):
            if cancel is not None and i % 10 == 0 and cancel():
                return []
            run = sim.run(random.Random(seed * 1_000_003 + i), my_next=p)
            values.append(run.my_value)
            # Also score the final roster on one possible "truth": each player's
            # real points drawn around your projection (same draw for every
            # candidate in this simulated draft), so the chance of ending better
            # also covers your projections being wrong, not just who the other
            # teams take. The average stays on your projections (no extra noise).
            drawn.append(team_value([_drawn(q, seed * 7919 + i) for q in run.my_roster], league))
        probs = avail.get(p.id, [])
        out.append(
            Recommendation(
                player=p,
                marginal_now=team_value(roster + [p], league) - base,
                p_avail_now=1.0 if on_clock else (probs[0] if probs else 0.0),
                p_avail_after=probs[1] if len(probs) > 1 else 0.0,
                exp_final_value=mean(values),
                values=values,
                drawn=drawn,
            )
        )
    out.sort(key=lambda r: r.exp_final_value, reverse=True)
    return out


def autodraft_value(state: DraftState, sims: int = 100, seed: int = 0, cancel=None) -> float:
    """Your average final roster value if ESPN autodrafted for you from here on,
    over the same simulated drafts `recommend` uses (same seeds), so a plan's
    value minus this is what the plan gains over autodraft."""
    if not state.my_next_picks():
        return 0.0
    sim = Simulator(state)
    values = []
    for i in range(sims):
        if cancel is not None and i % 10 == 0 and cancel():
            return 0.0
        values.append(sim.run(random.Random(seed * 1_000_003 + i), me_auto=True).my_value)
    return mean(values)


@dataclass
class VsBaseline:
    """A candidate compared with taking ESPN's top-projected player (the SAFE pick), draft by draft."""
    diff: float  # mean final-roster difference (+ = better than the baseline)
    p_better: float  # share of simulated drafts where it ends better
    avg_up: float  # average gain in those drafts
    p_worse: float
    avg_down: float  # average loss (negative) in drafts where it ends worse


def versus_highest_projection(recs: list[Recommendation]) -> tuple[Player | None, dict[str, VsBaseline]]:
    """Compare every candidate with the one ESPN projects highest, using the paired
    simulated drafts (same random seeds), so the split reflects the pick itself."""
    if not recs:
        return None, {}
    # Baseline: ESPN's top projection (what autodraft / ESPN's board would take).
    base = max(recs, key=lambda r: (r.player.room_points, r.player.proj_points))
    out = {}
    for r in recs:
        diffs = [a - b for a, b in zip(r.values, base.values)]
        if not diffs:
            continue
        # The odds (better / worse, by how much) from the drawn seasons when we
        # have them; the average from the projections themselves.
        odds = [a - b for a, b in zip(r.drawn, base.drawn)] if r.drawn and base.drawn else diffs
        up = [d for d in odds if d > 0.5]
        down = [d for d in odds if d < -0.5]
        out[r.player.id] = VsBaseline(
            diff=mean(diffs), p_better=len(up) / len(odds), avg_up=mean(up) if up else 0.0,
            p_worse=len(down) / len(odds), avg_down=mean(down) if down else 0.0)
    return base.player, out


def pick_gamble(recs: list[Recommendation], vs: dict[str, VsBaseline], baseline: Player | None,
                min_better: float = 0.5, min_diff: float = 30.0) -> Player | None:
    """The target (recs[0]) when it differs from ESPN's pick and the bet is worth
    it: on average at least `min_diff` better for your final roster, and better
    in at least `min_better` of the simulated drafts. Only the target can be a
    GAMBLE: a different "gamble" player could be worse than the target (e.g.
    one who will still be there next time while the target won't). None if
    the target is ESPN's pick or the edge is too small."""
    if baseline is None or not recs:
        return None
    top = recs[0]
    v = vs.get(top.player.id)
    if top.player.id == baseline.id or v is None or v.p_better < min_better or v.diff < min_diff:
        return None
    return top.player