import random
from pathlib import Path

import pytest

from fantasydraft.analyze import analyze
from fantasydraft.drafters import choose_pick, order_for
from fantasydraft.espn import EspnClient, parse_player
from fantasydraft.io import load_players, load_state, parse_positions, resolve_player
from fantasydraft.lineup import best_lineup, roster_value
from fantasydraft.models import DraftState, League, Player, TeamProfile
from fantasydraft.recommend import recommend
from fantasydraft.simulate import Simulator, availability

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "sample_players.csv"


def P(name, pos, pts, rank=None, adp=None):
    return Player(name, name, parse_positions(pos), pts,
                  float(rank) if rank else float("inf"), float(adp) if adp else float("inf"))


def league(n=4, my=0, strategies=None, slots=None, **kw):
    strategies = strategies or ["auto"] * n
    return League([TeamProfile(f"T{i}", s, noise=0.15) for i, s in enumerate(strategies)], my,
                  roster_slots=slots or {"PG": 1, "C": 1, "UTIL": 1, "BE": 1}, **kw)


def test_snake_order():
    lg = league(n=3)
    assert [lg.team_for_pick(p) for p in range(9)] == [0, 1, 2, 2, 1, 0, 0, 1, 2]
    assert lg.picks_for_team(0) == [0, 5, 6, 11]
    assert lg.total_picks == 12


def test_lineup_needs_matching_not_greedy_slotting():
    # Greedy slotting would put the PG/C into PG and leave C empty.
    slots = ["PG", "C"]
    a, b = P("a", "PG/C", 100), P("b", "PG", 90)
    starters, bench = best_lineup([a, b], slots)
    assert {p.name for p in starters} == {"a", "b"} and not bench


def test_roster_value_bench_weight():
    slots = ["C"]
    roster = [P("a", "C", 100), P("b", "C", 50), P("c", "PG", 40)]
    assert roster_value(roster, slots, 0.5) == pytest.approx(100 + 0.5 * 90)


def test_auto_drafter_takes_top_rank():
    players = [P("a", "PG", 10, rank=2), P("b", "C", 5, rank=1)]
    pick = choose_pick(TeamProfile("x", "auto"), order_for("auto", players), random.Random(0))
    assert pick.name == "b"


def test_favorites_pull_players_forward():
    players = [P(f"p{i}", "PG", 100 - i, rank=i + 1, adp=i + 1) for i in range(20)]
    prof = TeamProfile("x", "adp", noise=0.0, favorites={"p9": 9.5})
    assert choose_pick(prof, order_for("adp", players), random.Random(0)).name == "p9"


def _tiny_state(strategies, picks=(), my=0):
    players = {p.id: p for p in [
        P("pg1", "PG", 100, 1, 1), P("c1", "C", 95, 2, 2), P("pg2", "PG", 90, 3, 3),
        P("c2", "C", 60, 4, 4), P("pg3", "PG", 55, 5, 5), P("c3", "C", 30, 6, 6),
        P("u1", "SF", 50, 7, 7), P("u2", "SF", 45, 8, 8), P("u3", "SF", 20, 9, 9),
        P("u4", "SF", 10, 10, 10),
    ]}
    return DraftState(league(n=len(strategies), my=my, strategies=strategies), players, list(picks))


def test_availability_deterministic_with_auto_opponents():
    state = _tiny_state(["auto", "auto"], my=1)
    # T0 takes pg1 at pick 0, my picks are 1 and 2; opponents take nothing in between.
    probs = availability(state, sims=5)
    assert probs["pg1"] == [0.0, 0.0, 0.0, 0.0]
    assert probs["c1"][0] == 1.0 and probs["c1"][1] == 1.0
    # T0 picks again at 3 and 4 (taking c1 and pg2 if I pass on them)
    assert probs["c1"][2] == 0.0


def test_recommend_prefers_scarce_position():
    # Pick 1 of a 2-team draft. PGs are deep (100, 98, 96) but centers fall off
    # a cliff (95 -> 40), so taking the C first beats taking the top scorer.
    players = {p.id: p for p in [
        P("pg1", "PG", 100, 1), P("c1", "C", 95, 2), P("pg2", "PG", 98, 3),
        P("pg3", "PG", 96, 4), P("c2", "C", 40, 5), P("x1", "SF", 10, 6),
        P("x2", "SF", 9, 7), P("x3", "SF", 8, 8),
    ]}
    lg = league(n=2, strategies=["auto", "auto"], slots={"PG": 1, "C": 1, "BE": 2})
    recs = recommend(DraftState(lg, players), candidates=4, sims=3)
    assert recs[0].player.name == "c1"
    assert recs[0].p_avail_now == 1.0
    assert max(recs, key=lambda r: r.marginal_now).player.name == "pg1"


def test_simulation_completes_draft():
    state = load_state(SAMPLE, ROOT / "examples" / "league.json", None)
    result = Simulator(state).run(random.Random(1))
    assert len(result.my_roster) == state.league.rounds
    assert len(result.taken_at) == state.league.total_picks - state.league.rounds


def test_analyze_flags_autodrafter():
    state = _tiny_state(["auto", "points"])
    sim = Simulator(state)
    rng = random.Random(0)
    taken: set[str] = set()
    for pick in range(state.league.total_picks):
        prof = state.league.teams[state.league.team_for_pick(pick)]
        choice = choose_pick(prof, sim.top_untaken(prof.strategy, taken, 30), rng, pick)
        state.picks.append(choice.id)
        taken.add(choice.id)
    tendencies = analyze(state)
    assert tendencies[0].suggested == "auto"


def test_resolve_player_names():
    players = load_players(SAMPLE)
    assert players[resolve_player("player 001", players)].name == "Player 001"
    with pytest.raises(ValueError):
        resolve_player("Player 0", players)


def test_parse_espn_player_and_picks():
    raw = {
        "id": 3032977,
        "player": {
            "id": 3032977, "fullName": "Some Star", "defaultPositionId": 3,
            "eligibleSlots": [2, 3, 6, 8, 10, 11, 12, 13], "proTeamId": 15,
            "draftRanksByRankType": {"STANDARD": {"rank": 4}},
            "ownership": {"averageDraftPosition": 3.7},
            "stats": [
                {"seasonId": 2026, "statSourceId": 0, "statSplitTypeId": 0, "appliedTotal": 1.0},
                {"seasonId": 2027, "statSourceId": 1, "statSplitTypeId": 0, "appliedTotal": 3400.5},
            ],
        },
    }
    p = parse_player(raw, 2027)
    assert p.positions == frozenset({"SF", "PF"})
    assert (p.proj_points, p.espn_rank, p.adp) == (3400.5, 4.0, 3.7)

    league = {"draftDetail": {"picks": [
        {"overallPickNumber": 2, "teamId": 5, "playerId": 9, "autoDraftTypeId": 1},
        {"overallPickNumber": 1, "teamId": 3, "playerId": 8, "autoDraftTypeId": 0},
        {"overallPickNumber": 3, "teamId": 1, "playerId": -1},
    ]}}
    picks = EspnClient.picks(league)
    assert [(p.player_id, p.auto) for p in picks] == [("8", False), ("9", True)]


def test_sheet_links_become_csv_exports():
    from fantasydraft.io import sheet_csv_url

    base = "https://docs.google.com/spreadsheets/d/AbC-123_x"
    assert sheet_csv_url(base + "/edit?gid=0#gid=0") == base + "/export?format=csv&gid=0"
    assert sheet_csv_url(base + "/edit#gid=42") == base + "/export?format=csv&gid=42"
    assert sheet_csv_url(base + "/edit") == base + "/export?format=csv"
    assert sheet_csv_url("https://example.com/p.csv") == "https://example.com/p.csv"


def test_my_proj_and_ignore_columns(tmp_path):
    f = tmp_path / "p.csv"
    f.write_text(
        "id,name,positions,proj_points,espn_rank,adp,my_proj,ignore,notes\n"
        '1,A,PG,"1,500.5",1,2,,,\n'
        "2,B,C,1400,2,1,1600,,sleeper\n"
        "3,C,SF,1300,3,3,,x,hurt\n"
        "4,D,SF,,,,,,no projection yet\n"
        ",,,,,,,,\n"
    )
    players = load_players(f)
    assert set(players) == {"1", "2"}
    assert players["1"].proj_points == 1500.5
    assert players["2"].proj_points == 1600


def test_league_id_accepts_url():
    from fantasydraft.cli import _league_id

    assert _league_id("358709800") == 358709800
    assert _league_id("https://fantasy.espn.com/basketball/team?leagueId=358709800&seasonId=2027&teamId=4") == 358709800


def test_extract_picks_from_pasted_results():
    from fantasydraft.io import extract_picks

    players = {p.id: p for p in [
        P("Nikola Jokić", "C", 1), P("Jaren Jackson Jr.", "PF/C", 1), P("Jalen Williams", "SF", 1),
        P("Jaylin Williams", "PF", 1), P("Shai Gilgeous-Alexander", "PG", 1), P("Anthony Davis", "PF/C", 1),
    ]}
    text = """Round 1
    1. Team Bob - Nikola Jokic, Den C
    2  S. Gilgeous-Alexander  OKC PG   (Team Al)
    3 Jaren Jackson  MEM
    R2: Jalen Williams OKC | Anthony Davis DAL
    4 J. Williams    <- ambiguous, skipped
    recently drafted: Nikola Jokic
    """
    picks, warnings = extract_picks(text, players)
    assert picks == ["Nikola Jokić", "Shai Gilgeous-Alexander", "Jaren Jackson Jr.", "Jalen Williams", "Anthony Davis"]
    assert len(warnings) == 1


def test_autodraft_switches_keep_saved_strategy(tmp_path):
    from fantasydraft.io import apply_log, load_log

    state = _tiny_state(["adp", "points"])
    log = tmp_path / "picks.txt"
    log.write_text("pg1\n@auto T1\nc1\npg2\n@back 2\n")
    entries = load_log(log, state.players, state.league)
    apply_log(state, entries)
    assert state.picks == ["pg1", "c1", "pg2"]
    assert state.auto_picks == {1, 2}  # T1 picked twice while on auto (snake: 0,1,1,0)
    assert state.auto_teams == set()
    assert state.profile(1).strategy == "points"  # saved strategy restored

    state.auto_teams.add(1)
    assert state.profile(1).strategy == "auto"
    assert state.league.teams[1].strategy == "points"  # never overwritten


def test_simulation_uses_auto_while_flagged():
    state = _tiny_state(["points", "points"], my=0)
    state.picks = ["pg1"]
    state.auto_teams = {1}
    # On auto, T1 takes the best ESPN rank (c1) even with noise, every time.
    for seed in range(5):
        result = Simulator(state).run(random.Random(seed))
        assert result.taken_at["c1"] == 1


def test_analyze_skips_autodraft_picks():
    state = _tiny_state(["adp", "adp"])
    state.picks = ["pg1", "c1", "pg2", "c2"]
    state.auto_picks = {1, 2}
    t = analyze(state)
    assert (t[1].picks, t[1].auto_picks) == (0, 2)
    assert (t[0].picks, t[0].auto_picks) == (2, 0)


def test_player_by_rank():
    from fantasydraft.io import player_by_rank

    players = load_players(SAMPLE)
    pid = player_by_rank(1, players)
    assert players[pid].espn_rank == 1
    assert player_by_rank(99999, players) is None


def test_capture_redaction():
    from fantasydraft.capture import redact

    out = redact('{"token":"abc","x":1} SWID={ABCDEF} ' + "a" * 40)
    assert "abc" not in out and "ABCDEF" not in out and "a" * 40 not in out
    assert "SELECTED 3 4065648" == redact("SELECTED 3 4065648")


def _mock1_league():
    return League([TeamProfile(f"Team {i + 1}") for i in range(10)], my_slot=7)


def test_room_tracker_replays_real_espn_mock(tmp_path):
    from fantasydraft.io import apply_log, load_log
    from fantasydraft.room import RoomTracker

    league = _mock1_league()
    players = load_players(SAMPLE)
    picks = tmp_path / "picks.txt"
    # The capture started at pick 31; stand in for the first 30 picks.
    picks.write_text("".join(f"unknown:early{i}\n" for i in range(30)))
    lines = []
    tracker = RoomTracker(league, players, picks, log=lines.append)
    feed = (ROOT / "tests" / "data" / "espn_room_mock1.txt").read_text().splitlines()
    for msg in feed:
        if msg and not msg.startswith("#"):
            tracker.handle_message(msg)

    assert not tracker.blocked
    assert len(tracker.picks) == 129
    # ESPN team id -> draft position, as worked out from the page's pick list.
    assert {t: s + 1 for t, s in tracker.team_slot.items()} == {
        6: 1, 10: 2, 7: 3, 9: 4, 1: 5, 8: 6, 5: 7, 2: 8, 3: 9, 4: 10}
    assert tracker.my_team_id == 2

    state = DraftState(league, players)
    apply_log(state, load_log(picks, players, league))
    assert len(state.picks) == 129
    # The user: manual at 48, autodraft at 53, manual again from 68.
    assert 52 in state.auto_picks and 47 not in state.auto_picks and 67 not in state.auto_picks
    # Bob (7th) was on auto throughout; teams 3, 2 and 6 timed out and stayed on auto.
    assert state.auto_teams == {6, 2, 1, 5}
    assert any("ON autodraft" in line for line in lines)


def test_room_tracker_detects_wrong_pick_count(tmp_path):
    from fantasydraft.room import RoomTracker

    league = _mock1_league()
    lines = []
    tracker = RoomTracker(league, load_players(SAMPLE), tmp_path / "picks.txt", log=lines.append)
    # Joined mid-draft with no history: page says pick 31 is on the clock.
    tracker.handle_page_text("RND 4 OF 13\nON THE CLOCK: PICK 31\n")
    assert tracker.blocked
    tracker.handle_message("SELECTED 4 4066261 5 x")
    assert tracker.picks == []  # held, not written with the wrong team


def test_room_tracker_resyncs_from_page_pick_list(tmp_path):
    from fantasydraft.room import RoomTracker

    league = League([TeamProfile(f"Team {i + 1}") for i in range(2)], my_slot=0,
                    roster_slots={"PG": 1, "C": 1})
    players = {p.id: p for p in [P("Bam Adebayo", "C", 1), P("Stephen Curry", "PG", 1),
                                 P("Jalen Duren", "C", 1), P("Kyrie Irving", "PG", 1)]}
    tracker = RoomTracker(league, players, tmp_path / "picks.txt", log=lambda s: None)
    page = """Picks
Bam Adebayo / MIA C, PF
R1, P1 - Tarheels
Stephen Curry / GS PG
R1, P2 - JUSTICE FOR NOLAN
Jalen Duren / DET C
R2, P1 - JUSTICE FOR NOLAN
ON THE CLOCK: PICK 4
"""
    tracker.handle_page_text(page)
    assert not tracker.blocked
    assert tracker.picks == ["Bam Adebayo", "Stephen Curry", "Jalen Duren"]
    assert tracker.slot_names == {0: "Tarheels", 1: "JUSTICE FOR NOLAN"}


def test_room_tracker_flags_wrong_team_count(tmp_path):
    from fantasydraft.room import RoomTracker

    # League file says 10 teams, but the real draft has 8 (ids 1-8 in order).
    league = _mock1_league()
    lines = []
    tracker = RoomTracker(league, load_players(SAMPLE), tmp_path / "picks.txt", log=lines.append)
    order = list(range(1, 9)) + list(range(8, 0, -1))
    for i, team in enumerate(order):
        tracker.handle_message(f"SELECTING {team} 60000")
        tracker.handle_message(f"SELECTED {team} p{i} 1 member")
    assert tracker.blocked
    assert len(tracker.picks) == 8  # stopped at the first pick of round 2
    assert any("team count" in line for line in lines)


def test_percent_display():
    from fantasydraft.cli import _pct

    assert [_pct(x) for x in (0, 0.004, 0.012, 0.5, 0.995, 1)] == ["0%", "<1%", "1%", "50%", ">99%", "100%"]


def test_recommend_skips_players_who_never_last():
    # Pick 2 of a 2-team draft, not on the clock: the auto opponent takes pg1
    # first, so pg1 can never be mine and shouldn't be recommended.
    state = _tiny_state(["auto", "auto"], my=1)
    state.picks = []
    recs = recommend(state, candidates=4, sims=5)
    assert recs and all(r.p_avail_now > 0 for r in recs)
    assert "pg1" not in [r.player.name for r in recs]


def test_balance_targets_discount_extra_centers():
    from fantasydraft.lineup import over_targets, team_value

    lg = League([TeamProfile("a"), TeamProfile("b")], 0)  # default slots: 13 players, targets 3 each
    roster = [P("c1", "C", 2000), P("c2", "C", 1900), P("pfc", "PF/C", 1800)]
    c4, g = P("c4", "C", 1700), P("g", "PG", 1650)
    assert over_targets(roster + [c4], lg) == []  # PF/C counts as the PF
    roster.append(P("c5", "C", 1750))
    assert [p.name for p in over_targets(roster + [c4], lg)] == ["c4"]
    base = team_value(roster, lg)
    gain_c, gain_g = team_value(roster + [c4], lg) - base, team_value(roster + [g], lg) - base
    assert gain_c == pytest.approx(1700 * 0.8)
    assert gain_g == pytest.approx(1650)
    assert gain_g > gain_c

    no_balance = League([TeamProfile("a"), TeamProfile("b")], 0, balance_penalty=0.0)
    assert team_value(roster + [c4], no_balance) - team_value(roster, no_balance) == pytest.approx(1700)


def _simulated_draft(true_profiles, picks, seed=3):
    players = load_players(SAMPLE)
    lg = League(true_profiles, len(true_profiles) - 1)
    st = DraftState(lg, players)
    sim = Simulator(st)
    rng = random.Random(seed)
    taken: set[str] = set()
    for pick in range(picks):
        prof = true_profiles[lg.team_for_pick(pick)]
        c = choose_pick(prof, sim.top_untaken(prof.strategy, taken, 30), rng, pick)
        st.picks.append(c.id)
        taken.add(c.id)
    return players, st.picks


def test_learning_noise_and_strategy_from_picks():
    from fantasydraft.learn import fit_teams

    true = [TeamProfile("strict", "adp", 0.0), TeamProfile("loose rank", "rank", 0.4)] + \
        [TeamProfile(f"t{i}", "adp", 0.15) for i in range(8)]
    players, picks = _simulated_draft(true, 80)
    guess = League([TeamProfile(t.name, "adp", 0.2) for t in true], 9)
    fits = fit_teams(DraftState(guess, players, picks))
    assert fits[0].noise < 0.08  # always took the top of the ADP list
    assert fits[1].fit["rank"] > 0.8  # recognised as following ESPN rank
    assert 9 not in fits  # my own team is never fitted


def test_simulator_uses_learned_noise():
    true = [TeamProfile("strict", "adp", 0.0)] + [TeamProfile(f"t{i}", "adp", 0.15) for i in range(9)]
    players, picks = _simulated_draft(true, 40)
    guess = League([TeamProfile(t.name, "adp", 0.2) for t in true], 9)
    sim = Simulator(DraftState(guess, players, picks))
    assert sim.profile(0).noise < 0.12  # 4 picks, all top of the list: well below the 0.2 guess
    off = League([TeamProfile(t.name, "adp", 0.2) for t in true], 9, learn_noise=False)
    assert Simulator(DraftState(off, players, picks)).profile(0).noise == 0.2


def test_opponents_avoid_positions_they_have_filled():
    pool = [P("c", "C", 100, adp=50), P("g", "PG", 99, adp=53)] + \
        [P(f"x{i}", "SF", 50, adp=60 + i) for i in range(10)]
    prof = TeamProfile("x", "adp", noise=0.15)
    rng = random.Random(0)
    takes = lambda full: sum(choose_pick(prof, pool, rng, 49, full=full, bias=0.35).name == "c" for _ in range(2000))
    assert takes(frozenset()) > 600  # the C is usually taken when the team needs one
    assert takes(frozenset({"C"})) < 150  # rarely once they already have enough centers


def test_unknown_team_is_modelled_by_best_fit():
    true = [TeamProfile("rank guy", "rank", 0.05)] + [TeamProfile(f"t{i}", "adp", 0.15) for i in range(9)]
    players, picks = _simulated_draft(true, 60)
    guess = League([TeamProfile(t.name, "unknown", 0.2) for t in true], 9)
    sim = Simulator(DraftState(guess, players, picks))
    assert sim.profile(0).strategy == "rank"
    fresh = Simulator(DraftState(guess, players, []))
    assert fresh.profile(0).strategy == "adp"  # no picks yet: default guess


def test_feel_spreads_picks_over_espn_top():
    from collections import Counter

    pool = [P(f"r{i}", "PG", 100 - i, rank=i + 1) for i in range(20)]
    prof = TeamProfile("son", "feel", spread=8)
    rng = random.Random(1)
    got = Counter(choose_pick(prof, pool, rng).name for _ in range(4000))
    assert set(got) <= {f"r{i}" for i in range(8)}  # never outside the top 8
    assert 0.18 < got["r0"] / 4000 < 0.28  # #1 only mildly favoured
    assert got["r7"] / 4000 > 0.06


def test_balanced_skips_a_position_it_has_filled():
    pool = [P("c", "C", 2000), P("g", "PG", 1900)] + [P(f"x{i}", "SF", 1000 - i) for i in range(10)]
    prof = TeamProfile("dad", "balanced", noise=0.0)
    targets = {"PG": 3, "SG": 3, "SF": 3, "PF": 3, "C": 3}
    assert choose_pick(prof, pool, random.Random(0), 40, counts={"C": 1.0}, targets=targets).name == "c"
    assert choose_pick(prof, pool, random.Random(0), 40, counts={"C": 3.0}, targets=targets).name == "g"
