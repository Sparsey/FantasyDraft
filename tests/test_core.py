import random
from pathlib import Path

import pytest

from fantasydraft.analyze import analyze
from fantasydraft.drafters import choose_pick, order_for
from fantasydraft.espn import EspnClient, parse_player
from fantasydraft.io import load_players, load_state, parse_positions, resolve_player
from fantasydraft.lineup import best_lineup, roster_value
from fantasydraft.models import DraftState, League, Player, TeamProfile
from fantasydraft.recommend import Recommendation, recommend
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
    tracker.handle_page_text("ON THE CLOCK: PICK 31")  # the page confirms the 30 picks we resume with
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
    # the pick carousel: round 2 starts at pick 9
    tracker.handle_page_text("ON THE CLOCK: PICK 1\nTeam A\nPICK 2\nTeam B\nROUND\n2\nPICK 9\nTeam H")
    assert tracker.team_count == 8
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

    lg = League([TeamProfile("a"), TeamProfile("b")], 0, cover_rate=0)  # default slots: 13 players, targets 3 each
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

    no_balance = League([TeamProfile("a"), TeamProfile("b")], 0, balance_penalty=0.0, cover_rate=0)
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
    assert max(fits[1].fit, key=fits[1].fit.get) == "rank"  # recognised as following ESPN rank
    assert fits[1].fit["rank"] > 0.5
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


def test_predict_picks_autodrafter_is_certain():
    from fantasydraft.simulate import predict_picks

    state = _tiny_state(["auto", "adp"], my=1)
    preds = predict_picks(state, sims=20, horizon=3)
    assert [p["pick"] for p in preds] == [0, 1, 2]
    assert preds[0]["probs"] == {"pg1": 1.0}  # autodraft takes the top ESPN rank
    assert abs(sum(preds[1]["probs"].values()) - 1.0) < 1e-9


def test_rank_vs_projection_detects_identical_order():
    import dataclasses
    from fantasydraft.benchmark import rank_vs_projection

    players = load_players(SAMPLE)
    assert "IDENTICAL" not in rank_vs_projection(players)
    order = sorted(players.values(), key=lambda p: -p.proj_points)
    same = {p.id: dataclasses.replace(p, espn_rank=float(i + 1)) for i, p in enumerate(order)}
    assert "IDENTICAL" in rank_vs_projection(same)


def test_draft_room_ranks_from_capture_and_merge():
    import json as _json
    from fantasydraft.espn import draft_ranks_from_capture, merge_draft_ranks, parse_player

    body = {"players": [{"id": 7, "player": {"id": 7, "fullName": "A B",
                                             "draftRanksByRankType": {"STANDARD": {"rank": 4}, "ROTO": {"rank": 9}}}}]}
    events = [{"kind": "xhr", "url": "x?view=kona_player_info_edit_draft_strategy", "data": _json.dumps(body)},
              {"kind": "ws-in", "data": "CLOCK 1 2 3"}]
    assert draft_ranks_from_capture(events) == {"7": {"name": "A B", "STANDARD": 4, "ROTO": 9}}

    info = [{"id": 7, "player": {"id": 7, "fullName": "A B", "eligibleSlots": [0], "draftRanksByRankType": {}}}]
    assert merge_draft_ranks(info, body["players"]) == 1
    assert parse_player(info[0], 2027).espn_rank == 4.0


def test_hashtag_style_source_to_league_points():
    from fantasydraft.sources import apply_sources, clean_name, read_source

    assert clean_name("Shai Gilgeous-AlexanderS.Gilgeous-Alexander") == "Shai Gilgeous-Alexander"
    assert clean_name("Karl-Anthony TownsK.Towns") == "Karl-Anthony Towns"
    src = str(ROOT / "tests" / "data" / "hashtag_sample.csv")
    lines = read_source(src)
    assert [l.name for l in lines][:2] == ["Nikola Jokic", "Victor Wembanyama"]  # repeated header skipped
    assert len(lines) == 5
    jokic = lines[0]
    assert jokic.stats["3PM"] == pytest.approx(1.8 * 72) and jokic.stats["FGA"] == pytest.approx(18.3 * 72)

    players = {"j": Player("j", "Nikola Jokić", frozenset({"C"}), 4838.0, 1, espn_points=4838.0),
               "w": Player("w", "Victor Wembanyama", frozenset({"C"}), 4000.0, 4, espn_points=4000.0),
               "x": Player("x", "Nobody Else", frozenset({"PG"}), 1000.0, 300, espn_points=1000.0, overridden=False)}
    reports = apply_sources(players, {"hashtag": src})
    assert reports[0].matched == 2 and not reports[0].calibrated
    # Jokic: ESPN 4838, Hashtag ~4853 under ESPN default points scoring -> consensus ~4845
    assert players["j"].proj_points == pytest.approx((4838 + 67.4 * 72) / 2, abs=5)
    assert players["j"].room_points == 4838.0  # opponents still see ESPN
    assert players["x"].proj_points == 1000.0 and len(players["x"].sources) == 1


def test_basketball_reference_style_source(tmp_path):
    from fantasydraft.sources import fantasy_points, read_source

    f = tmp_path / "bbref.csv"
    f.write_text(
        "Rk,Player,Age,Team,Pos,G,GS,MP,FG,FGA,FG%,3P,3PA,3P%,2P,2PA,2P%,eFG%,FT,FTA,FT%,ORB,DRB,TRB,AST,STL,BLK,TOV,PF,PTS\n"
        "1,Nikola Jokić*,30,DEN,C,70,70,36.7,11.2,19.5,.576,2.0,4.7,.417,9.2,14.8,.623,.627,5.5,6.6,.833,2.9,9.8,12.7,10.2,1.8,0.6,3.3,2.3,29.9\n"
        "2,Some Traded,25,2TM,SF,60,10,20,3.0,7.0,.43,1,3,.33,2,4,.5,.5,1,1.2,.8,1,2,3,1,0.5,0.2,1,1,8.0\n"
        "3,Some Traded,25,AAA,SF,30,5,20,3.0,7.0,.43,1,3,.33,2,4,.5,.5,1,1.2,.8,1,2,3,1,0.5,0.2,1,1,8.0\n"
        "Rk,Player,Age,Team,Pos,G,GS,MP,FG,FGA,FG%,3P,3PA,3P%,2P,2PA,2P%,eFG%,FT,FTA,FT%,ORB,DRB,TRB,AST,STL,BLK,TOV,PF,PTS\n"
    )
    lines = read_source(str(f))
    assert [l.name for l in lines] == ["Nikola Jokić", "Some Traded"]
    j = lines[0].stats
    assert j["FGM"] == pytest.approx(11.2 * 70) and j["FGA"] == pytest.approx(19.5 * 70)
    pts, missing = fantasy_points(j, {"PTS": 1, "FGM": 2, "FGA": -1, "TO": -2})
    assert not missing


def test_source_from_html_page(tmp_path):
    from fantasydraft.sources import read_source

    f = tmp_path / "page.html"
    f.write_text("""<html><body>
    <table><tr><td>menu</td></tr></table>
    <table id="per_game_stats"><thead><tr><th>Rk</th><th>Player</th><th>Team</th><th>G</th><th>FG</th><th>FGA</th>
      <th>FG%</th><th>3P</th><th>FT</th><th>FTA</th><th>TRB</th><th>AST</th><th>STL</th><th>BLK</th><th>TOV</th><th>PTS</th></tr></thead>
    <tbody><tr><th>1</th><td><a href="#">Nikola Jokić</a>*</td><td>DEN</td><td>70</td><td>11.2</td><td>19.5</td><td>.576</td>
      <td>2.0</td><td>5.5</td><td>6.6</td><td>12.7</td><td>10.2</td><td>1.8</td><td>0.6</td><td>3.3</td><td>29.9</td></tr>
    <tr class="thead"><th>Rk</th><th>Player</th><th>Team</th><th>G</th><th>FG</th><th>FGA</th><th>FG%</th><th>3P</th>
      <th>FT</th><th>FTA</th><th>TRB</th><th>AST</th><th>STL</th><th>BLK</th><th>TOV</th><th>PTS</th></tr></tbody></table>
    <!-- <table><tr><th>Player</th><th>PTS</th></tr></table> -->
    </body></html>""")
    lines = read_source(str(f))
    assert [l.name for l in lines] == ["Nikola Jokić"]
    assert lines[0].stats["FGM"] == pytest.approx(11.2 * 70)


def test_source_weights_and_projected_games(tmp_path):
    from fantasydraft.sources import apply_sources, clean_name

    assert clean_name("Michael Porter Jr.(BKN - SF,PF)DTD") == "Michael Porter Jr."
    proj = tmp_path / "proj.csv"
    proj.write_text("PLAYER,GP,PTS,REB,AST,STL,BLK,TO,3PM\nInjured Star,70,30,5,5,1,1,3,2\n")
    last = tmp_path / "last.csv"  # played only 10 games last season
    last.write_text("Player,G,PTS,TRB,AST,STL,BLK,TOV,3P\nInjured Star,10,30,5,5,1,1,3,2\n")
    scoring = {"PTS": 1}
    players = {"s": Player("s", "Injured Star", frozenset({"PG"}), 2100.0, 1, espn_points=2100.0)}
    apply_sources(players, {"proj": str(proj),
                            "last": {"url": str(last), "weight": 0.5, "games": "projected"}}, scoring)
    vals = dict(players["s"].sources)
    assert vals["last"] == pytest.approx(30 * 70)  # per game x projected 70 games, not x 10
    # weighted: (2100*1 + 2100*1 + 2100*0.5) / 2.5
    assert players["s"].proj_points == pytest.approx(2100)


def test_estimate_shooting_from_percentages():
    from fantasydraft.sources import estimate_shooting

    # Jokic per game: 28.4 pts, 1.8 3PM, .573 FG, .816 FT; real 10.5/18.3 FG, 5.6/6.8 FT
    est = estimate_shooting({"PTS": 28.4, "3PM": 1.8}, {"FG%": 0.573, "FT%": 0.816}, ft_rate=6.8 / 18.3)
    assert est["FGA"] == pytest.approx(18.3, abs=0.2) and est["FGM"] == pytest.approx(10.5, abs=0.15)
    assert est["FTA"] == pytest.approx(6.8, abs=0.1) and est["FTM"] == pytest.approx(5.6, abs=0.1)
    assert 2 * est["FGM"] + 1.8 + est["FTM"] == pytest.approx(28.4)


def test_percentage_only_source_no_longer_rescaled(tmp_path):
    from fantasydraft.sources import apply_sources

    f = tmp_path / "fp.csv"
    f.write_text("Player,PTS,REB,AST,BLK,STL,FG%,FT%,3:00 PM,GP,MIN,TO\n"
                 "Big Man,15,10,2,1.5,0.5,0.62,0.70,0.0,70,28,1.5\n"
                 "Volume Guard,15,3,5,0.2,1.0,0.41,0.85,2.5,70,32,2.5\n")
    players = {"b": Player("b", "Big Man", frozenset({"C"}), 2000.0, 1, espn_points=2000.0),
               "g": Player("g", "Volume Guard", frozenset({"PG"}), 2000.0, 2, espn_points=2000.0)}
    reports = apply_sources(players, {"fp": str(f)})
    assert reports[0].estimated == 2 and not reports[0].calibrated and not reports[0].missing_stats
    vals_b, vals_g = dict(players["b"].sources)["fp"], dict(players["g"].sources)["fp"]
    # Same points, but the efficient big gains from the FGM/FGA scoring and the volume guard loses.
    big_shooting = 2 * 0.62 - 1  # per FGA
    assert big_shooting > 0 and vals_b > vals_g


def test_dashboard_advice_explains_skipping_highest_projection():
    from fantasydraft.ui import Dashboard

    def Q(n, pos, pts, rank):
        return Player(n, n, frozenset(pos.split("/")), float(pts), float(rank), float(rank))

    pl = [Q("Filler 0", "SF", 900, 1), Q("Guard A", "PG", 3000, 2), Q("Center A", "C", 2000, 3),
          Q("Filler 1", "SF", 800, 4), Q("Filler 2", "SF", 790, 5), Q("Filler 3", "SF", 780, 6),
          Q("Guard B", "PG", 2500, 7), Q("Center B", "C", 1000, 8)] + [Q(f"f{i}", "SF", 300 - i, 9 + i) for i in range(12)]
    players = {p.id: p for p in pl}
    teams = [TeamProfile("Opp1", "auto"), TeamProfile("Me", "adp"), TeamProfile("Opp2", "auto"), TeamProfile("Opp3", "auto")]
    lg = League(teams, 1, roster_slots={"PG": 1, "C": 1, "BE": 2}, balance_penalty=0, cover_rate=0)
    st = DraftState(lg, players, ["Filler 0"])
    from fantasydraft.simulate import availability

    recs = recommend(st, candidates=4, sims=5)
    advice = Dashboard._advice(st, recs, availability(st, sims=20))
    assert advice["kind"] == "different"
    assert advice["top"] == "Center A" and advice["best"] == "Guard A" and advice["gain"] >= 200
    assert any("Guard B" in r and "Center B" in r for r in advice["reasons"]), advice["reasons"]


def test_managers_attach_to_teams_by_name(tmp_path):
    import json as _json
    from fantasydraft.io import load_league

    cfg = {
        "my_team": 1,
        "managers": {
            "Ryan": {"match": ["Running Rhinos", "RRT"], "strategy": "balanced", "noise": 0.08, "view": "consensus"},
            "Logan": {"match": ["Waiver Warrior"], "strategy": "feel", "spread": 8, "favorite_team": "lal"},
        },
        "teams": [{"name": "Me"}, {"name": "Running Rhinos", "noise": 0.2},
                  {"name": "Team 3", "owner": "Logan"}, {"name": "Team 4", "strategy": "rank"}],
    }
    f = tmp_path / "l.json"
    f.write_text(_json.dumps(cfg))
    lg = load_league(f)
    assert (lg.teams[1].strategy, lg.teams[1].noise, lg.teams[1].view, lg.teams[1].manager) == ("balanced", 0.08, "consensus", "Ryan")
    assert (lg.teams[2].strategy, lg.teams[2].favorite_team, lg.teams[2].team_boost) == ("feel", "LAL", 6.0)
    assert lg.teams[3].strategy == "rank" and lg.teams[3].manager == ""


def test_consensus_view_drafter_is_recognised():
    import dataclasses
    from fantasydraft.learn import fit_teams

    base = load_players(SAMPLE)
    rng = random.Random(5)
    # My consensus differs from ESPN by up to +-15%.
    players = {pid: dataclasses.replace(p, espn_points=p.proj_points,
                                        proj_points=p.proj_points * rng.uniform(0.85, 1.15))
               for pid, p in base.items()}
    true = [TeamProfile("homework", "points", 0.04, view="consensus")] + \
        [TeamProfile(f"t{i}", "adp", 0.15) for i in range(9)]
    lg = League(true, 9)
    st = DraftState(lg, players)
    sim = Simulator(st)
    rng2 = random.Random(1)
    taken: set[str] = set()
    for pick in range(100):
        prof = true[lg.team_for_pick(pick)]
        c = choose_pick(prof, sim.top_untaken(prof.order_key, taken, 30), rng2, pick)
        st.picks.append(c.id)
        taken.add(c.id)
    guess = League([TeamProfile(t.name, "unknown", 0.2) for t in true], 9)
    fit = fit_teams(DraftState(guess, players, st.picks))[0]
    assert max(fit.fit, key=fit.fit.get) in ("points@consensus", "balanced@consensus")


def test_history_from_past_draft():
    from fantasydraft.history import analyze_draft, merge_managers

    base = sorted(load_players(SAMPLE).values(), key=lambda p: p.espn_rank)[:80]
    teams_nba = ["LAL", "BOS", "DEN", "MIA"]
    raw_players = [{"id": int(p.id), "player": {
        "id": int(p.id), "fullName": p.name, "eligibleSlots": [0], "proTeamId": 13 if p.name == "Player 012" else 2 + i % 3,
        "draftRanksByRankType": {"STANDARD": {"rank": p.espn_rank}}, "ownership": {"averageDraftPosition": p.adp},
        "stats": [{"seasonId": 2026, "statSourceId": 1, "statSplitTypeId": 0, "appliedTotal": p.proj_points}]}}
        for i, p in enumerate(base)]
    # 4 teams; team 10 autodrafts (takes top ESPN rank), team 20 takes the 2nd-best rank each time.
    order = [10, 20, 30, 40]
    lg = League([TeamProfile(str(t)) for t in order], 0, roster_slots={"PG": 1, "UTIL": 4})
    avail = list(base)
    picks = []
    for k in range(lg.total_picks):
        tid = order[lg.team_for_pick(k)]
        avail.sort(key=lambda p: p.espn_rank)
        p = avail.pop(1 if tid == 20 and len(avail) > 1 else 0)
        picks.append({"overallPickNumber": k + 1, "teamId": tid, "playerId": int(p.id), "autoDraftTypeId": 1 if tid == 10 else 0})
    data = {
        "settings": {"draftSettings": {"pickOrder": order},
                     "rosterSettings": {"lineupSlotCounts": {"0": 1, "11": 4}}},
        "teams": [{"id": t, "name": f"Team {t}", "abbrev": f"T{t}", "owners": [f"m{t}"]} for t in order],
        "members": [{"id": f"m{t}", "firstName": f"Person{t}", "lastName": "X"} for t in order],
        "draftDetail": {"picks": picks},
    }
    rows = analyze_draft(data, raw_players, 2026, me="Person40")
    by = {h.owner: h for h in rows}
    assert by["Person10 X"].auto_picks == 5 and by["Person10 X"].profile()["strategy"] == "auto"
    assert by["Person20 X"].picks == 5 and by["Person20 X"].top_rank_rate == 0.0
    merged = merge_managers({"Person20 X": {"strategy": "feel"}}, rows)
    assert merged["Person20 X"]["strategy"] == "feel"  # hand-set value kept
    assert "T20" in merged["Person20 X"]["match"]


def test_missed_picks_entered_by_hand_release_held_feed(tmp_path):
    from fantasydraft.room import RoomTracker

    league = _mock1_league()  # 10 teams; ESPN team id = draft position here
    players = load_players(SAMPLE)
    ids = sorted(players, key=lambda pid: players[pid].espn_rank)
    tracker = RoomTracker(league, players, tmp_path / "picks.txt", log=lambda s: None)
    tracker.handle_page_text("ON THE CLOCK: PICK 1")

    def feed(pick):
        team = league.team_for_pick(pick) + 1
        tracker.handle_message(f"SELECTING {team} 60000")
        tracker.handle_message(f"SELECTED {team} {ids[pick]} 1 member")

    for k in range(12):
        feed(k)
    # Page reload: picks 13 and 14 (index 12, 13) are missed, then 15 and 16 arrive.
    feed(14)
    feed(15)
    tracker.handle_page_text("ON THE CLOCK: PICK 17")
    assert tracker.blocked
    assert tracker.missing() == (13, 2)
    assert len(tracker.picks) == 12
    tracker.manual_pick(ids[12])
    assert tracker.blocked  # one still missing
    tracker.manual_pick(ids[13])
    assert not tracker.blocked
    assert tracker.picks == ids[:16]  # held picks 15 and 16 replayed in place


def test_stale_recommendations_drop_drafted_players(tmp_path):
    from fantasydraft.ui import Dashboard

    f = tmp_path / "l.json"
    f.write_text('{"my_team": 4, "players": "%s", "teams": [%s]}' % (
        str(SAMPLE).replace("\\\\", "/"), ",".join('{"name": "T%d"}' % i for i in range(10))))
    dash = Dashboard(None, f, tmp_path / "p.txt", None, sims=5, candidates=4)
    st = dash.state()
    top = sorted(st.players.values(), key=lambda p: -p.proj_points)[:3]
    dash.result = {"version": dash.tracker.version, "recs": [{"id": p.id, "name": p.name} for p in top],
                   "advice": {"top": top[0].name, "best": top[0].name}}
    dash.tracker.manual_pick(top[0].id)  # a pick lands before the recompute
    r = dash._current_result(dash.state())
    assert r["stale"] and [x["id"] for x in r["recs"]] == [p.id for p in top[1:]]
    assert r["advice"] is None


def test_versus_highest_projection_split():
    from fantasydraft.recommend import versus_highest_projection

    def Q(n, pos, pts, rank):
        return Player(n, n, frozenset(pos.split("/")), float(pts), float(rank), float(rank))

    pl = [Q("Filler 0", "SF", 900, 1), Q("Guard A", "PG", 3000, 2), Q("Center A", "C", 2000, 3),
          Q("Filler 1", "SF", 800, 4), Q("Filler 2", "SF", 790, 5), Q("Filler 3", "SF", 780, 6),
          Q("Guard B", "PG", 2500, 7), Q("Center B", "C", 1000, 8)] + [Q(f"f{i}", "SF", 300 - i, 9 + i) for i in range(12)]
    players = {p.id: p for p in pl}
    teams = [TeamProfile("Opp1", "auto"), TeamProfile("Me", "adp"), TeamProfile("Opp2", "auto"), TeamProfile("Opp3", "auto")]
    lg = League(teams, 1, roster_slots={"PG": 1, "C": 1, "BE": 2}, balance_penalty=0, cover_rate=0)
    recs = recommend(DraftState(lg, players, ["Filler 0"]), candidates=4, sims=8)
    base, vs = versus_highest_projection(recs)
    assert base.name == "Guard A" and vs["Guard A"].diff == 0
    c = vs["Center A"]
    # a clear edge, but not a certainty once projections can be wrong
    assert c.diff >= 200 and c.p_better >= 0.75 and c.p_better > c.p_worse


def test_safe_baseline_is_espns_top_projection():
    from dataclasses import replace
    from fantasydraft.recommend import versus_highest_projection

    def R(name, mine, espn):
        return Recommendation(replace(Player(name, name, frozenset({"PG"}), mine), espn_points=espn), 0, 1, 0, 0, [mine])
    recs = [R("consensus top", 3000.0, 2500.0), R("espn top", 2800.0, 2900.0)]
    base, vs = versus_highest_projection(recs)
    assert base.name == "espn top" and vs["consensus top"].diff == 200


def test_gamble_is_only_ever_the_target():
    from fantasydraft.recommend import VsBaseline, pick_gamble

    def R(name):
        return Recommendation(Player(name, name, frozenset({"PG"}), 1000.0), 0, 1, 0, 0)
    espn, target, other = R("espn"), R("target"), R("other")
    vs = {"espn": VsBaseline(0, 0, 0, 0, 0), "target": VsBaseline(+80, 0.62, 250, 0.38, -190),
          "other": VsBaseline(+60, 0.70, 200, 0.30, -100)}
    assert pick_gamble([target, other, espn], vs, espn.player).name == "target"
    # a different player is never tagged, even if he beats ESPN's pick
    assert pick_gamble([espn, other, target], vs, espn.player) is None
    vs["target"] = VsBaseline(+80, 0.45, 300, 0.55, -100)  # better on average, worse more often than not
    assert pick_gamble([target, other, espn], vs, espn.player) is None
    vs["target"] = VsBaseline(+10, 0.55, 100, 0.45, -90)  # too small to be worth it
    assert pick_gamble([target, other, espn], vs, espn.player) is None


def test_auto_in_league_file_is_the_autodraft_switch(tmp_path):
    from fantasydraft.io import apply_log, load_league
    lg = load_league("examples/league.json")
    frank = next(i for i, t in enumerate(lg.teams) if t.name == "Frank")
    assert frank in lg.start_auto and lg.teams[frank].strategy == "rank"
    st = DraftState(lg, {})
    assert frank in st.auto_teams and st.profile(frank).strategy == "auto"
    apply_log(st, [("auto", frank, False)])  # switched off in the room
    assert st.profile(frank).strategy == "rank"


def test_recent_picks_count_more_and_unsure_teams_are_mixed():
    import fantasydraft.learn as learn

    rows = [{"adp": (0.9,) * 9, "rank": (0.1,) * 9}] * 6 + [{"adp": (0.1,) * 9, "rank": (0.9,) * 9}] * 6
    real = learn._pick_evidence
    learn._pick_evidence = lambda state: {0: rows}
    try:
        lg = League([TeamProfile("switcher", "unknown")] + [TeamProfile(f"t{i}", "adp") for i in range(9)], 9)
        f = learn.fit_teams(DraftState(lg, {}))[0]
    finally:
        learn._pick_evidence = real
    assert f.fit["rank"] > 0.9  # same number of picks each way: the recent ones win
    assert f.strategy == "rank" and set(f.mix) <= {"rank", "adp"}


def test_simulator_draws_unsure_teams_strategy_per_draft():
    lg = League([TeamProfile("u", "unknown")] + [TeamProfile(f"t{i}", "adp") for i in range(9)], 9)
    sim = Simulator(DraftState(lg, {}))
    sim.mixes = {0: [(0.6, TeamProfile("u", "rank")), (0.4, TeamProfile("u", "feel"))]}
    rng = random.Random(1)
    draws = [sim.draw_profiles(rng)[0].strategy for _ in range(2000)]
    assert 0.55 < draws.count("rank") / 2000 < 0.65


def test_rolling_draft_score():
    from fantasydraft.analyze import draft_scores

    pl = {n: P(n, "PG", pts) for n, pts in (("a", 1000), ("b", 900), ("c", 800), ("d", 700))}
    lg = League([TeamProfile("x"), TeamProfile("y")], 0, roster_slots={"PG": 1, "BE": 1})
    st = DraftState(lg, pl, ["b", "a", "c"])  # x reached for b; a fell to y
    s = draft_scores(st)
    assert s[0]["value"] == -100 + 0 and s[1]["value"] == +100
    assert s[0]["espn_value"] == -100 and s[1]["espn_value"] == +100  # no sources: same as ESPN
    assert s[0]["starters"] == 900 and s[1]["starters"] == 1000


def test_room_restart_waits_for_page_before_mapping_new_teams(tmp_path):
    from fantasydraft.room import RoomTracker

    players = load_players(SAMPLE)
    ids = sorted(players, key=lambda p: players[p].espn_rank)
    league = League([TeamProfile(f"Team {i + 1}") for i in range(10)], my_slot=7)
    espn = [3, 7, 1, 9, 4, 10, 2, 8, 5, 6]  # ESPN team id of each draft slot
    picks = tmp_path / "p.txt"
    picks.write_text("".join(f"{ids[k]}\n" for k in range(12)))
    tr = RoomTracker(league, players, picks, log=lambda _: None)
    for k in (14, 15):  # restarted after pick 12; picks 13 and 14 were missed
        t = espn[league.team_for_pick(k)]
        tr.handle_message(f"SELECTING {t} 60000")
        tr.handle_message(f"SELECTED {t} {ids[k]} 1 m")
    assert len(tr.picks) == 12 and tr.blocked
    tr.handle_page_text("ON THE CLOCK: PICK 17")
    assert tr.missing() == (13, 2)
    tr.manual_pick(ids[12])
    tr.manual_pick(ids[13])
    assert not tr.blocked and tr.picks == ids[:16]
    assert tr.team_slot == {espn[league.team_for_pick(14)]: league.team_for_pick(14),
                            espn[league.team_for_pick(15)]: league.team_for_pick(15)}
    # the mapping survives another restart
    again = RoomTracker(league, players, picks, log=lambda _: None)
    assert again.team_slot == tr.team_slot


def test_room_resync_keeps_mapping_and_autodraft(tmp_path):
    from fantasydraft.room import RoomTracker

    players = load_players(SAMPLE)
    ids = sorted(players, key=lambda p: players[p].espn_rank)
    league = League([TeamProfile(f"Team {i + 1}") for i in range(10)], my_slot=7)
    espn = [3, 7, 1, 9, 4, 10, 2, 8, 5, 6]
    tr = RoomTracker(league, players, tmp_path / "p.txt", log=lambda _: None)
    tr.handle_page_text("ON THE CLOCK: PICK 1")
    for k in range(12):
        t = espn[league.team_for_pick(k)]
        tr.handle_message(f"SELECTING {t} 60000")
        tr.handle_message(f"SELECTED {t} {ids[k]} 1" + ("" if k == 3 else " m"))  # pick 4 by autodraft
    assert league.team_for_pick(3) in tr.auto_slots()
    # page reload: picks 13-14 missed; 15-16 arrive; then the page's pick list
    for k in (14, 15):
        t = espn[league.team_for_pick(k)]
        tr.handle_message(f"SELECTING {t} 60000")
        tr.handle_message(f"SELECTED {t} {ids[k]} 1 m")
    assert tr.blocked
    n = league.num_teams
    lines = [f"{players[ids[k]].name} / BOS PG\nR{k // n + 1}, P{k % n + 1} - Team" for k in range(16)]
    tr.handle_page_text("\n".join(lines) + "\nON THE CLOCK: PICK 17")
    assert not tr.blocked and tr.picks == ids[:16]
    assert league.team_for_pick(3) in tr.auto_slots()  # still on autodraft


def test_players_sheet_problems_are_reported_not_fatal(tmp_path, capsys):
    import pytest
    from fantasydraft.io import load_players

    html = tmp_path / "login.csv"
    html.write_text("<!DOCTYPE html><html>Sign in</html>")
    with pytest.raises(ValueError, match="Anyone with the link"):
        load_players(html)
    f = tmp_path / "p.csv"
    f.write_text("﻿id,name,positions,proj_points,espn_rank,adp,notes\n"
                 "1,A One,PG,2000,1,N/A,ok\n"
                 "2,B Two,XX,1900,2,2,bad position\n"
                 "3,C Three,C,1800,3,-,a, stray comma\n"
                 "4,A One,PG,1700,4,4,same name\n", encoding="utf-8")
    players = load_players(f)
    assert set(players) == {"1", "3", "4"} and players["1"].adp == float("inf")
    err = capsys.readouterr().err
    assert "left out 1 rows" in err and "same player name" in err


def test_name_and_link_normalising():
    from fantasydraft.io import _norm, sheet_csv_url
    from fantasydraft.sources import _key

    assert _norm("P.J. Washington") == _norm("PJ Washington") == "pj washington"
    assert _norm("Jaren Jackson Jr.") == "jaren jackson"
    assert sheet_csv_url("https://docs.google.com/spreadsheets/d/e/2PACX-x/pubhtml?gid=5") == \
        "https://docs.google.com/spreadsheets/d/e/2PACX-x/pub?output=csv&gid=5"
    assert _key("3P%") is None and _key("TOV%") is None and _key("FG%") == "FG%" and _key("3PM") == "3PM"


def test_hand_entry_of_unlisted_player(tmp_path):
    from fantasydraft.io import load_log
    from fantasydraft.room import RoomTracker

    players = load_players(SAMPLE)
    league = League([TeamProfile(f"T{i}") for i in range(10)], 0)
    tr = RoomTracker(league, players, tmp_path / "p.txt", log=lambda _: None)
    tr.manual_pick(name="Some Rookie")
    assert len(tr.picks) == 1
    assert len(load_log(tmp_path / "p.txt", dict(players), league)) == 1  # survives a restart


def test_fetch_league_keeps_your_edits(tmp_path, monkeypatch):
    import argparse
    import json
    import fantasydraft.espn as espn
    from fantasydraft.cli import cmd_fetch_league

    order = [[11, 12, 13], [13, 11, 12]]  # ESPN reshuffles the draft order

    class Fake:
        def __init__(self, *a):
            pass

        def league(self):
            return {}

        team_names = staticmethod(lambda d: {11: "Alpha", 12: "Bravo v2", 13: "Mine"})
        team_owners = staticmethod(lambda d: {13: "Aaron Solomon"})
        pick_order = staticmethod(lambda d: order[0])
        roster_slots = staticmethod(lambda d: {"PG": 1, "BE": 1})
        scoring = staticmethod(lambda d: {})

    monkeypatch.setattr(espn, "EspnClient", Fake)
    out = tmp_path / "league.json"
    out.write_text(json.dumps({
        "players": "sheet", "my_team": 3, "position_targets": {"C": 2}, "sources": {"fp": "x"},
        "teams": [{"name": "Alpha", "strategy": "adp"}, {"name": "Bravo", "espn_id": 12, "strategy": "feel"},
                  {"name": "Mine"}]}))
    args = argparse.Namespace(league_id=1, season=2027, espn_s2=None, swid=None, dump=None, out=str(out))
    cmd_fetch_league(args)
    order.reverse()
    cmd_fetch_league(args)
    cfg = json.loads(out.read_text())
    assert cfg["position_targets"] == {"C": 2} and cfg["sources"] == {"fp": "x"} and cfg["players"] == "sheet"
    assert [t["name"] for t in cfg["teams"]] == ["Mine", "Alpha", "Bravo v2"]
    assert cfg["my_team"] == "Mine"
    bravo = cfg["teams"][2]
    assert bravo["strategy"] == "feel" and bravo["espn_id"] == 12  # renamed on ESPN, still matched


def test_wrong_team_count_is_detected_and_restart_recovers(tmp_path):
    from fantasydraft.room import RoomTracker

    players = load_players(SAMPLE)
    ids = sorted(players, key=lambda p: players[p].espn_rank)
    espn = [21, 22, 23, 24, 25, 26, 27, 28]  # the real draft has 8 teams; I'm 3rd (team 23)
    real = League([TeamProfile(f"Team {i + 1}") for i in range(8)], 2)
    ten = League([TeamProfile(f"Team {i + 1}") for i in range(10)], 2)
    picks = tmp_path / "mock1.txt"
    tr = RoomTracker(ten, players, picks, log=lambda _: None)
    tr.handle_page_text("ON THE CLOCK: PICK 1\nROUND\n2\nPICK 9")
    tr.handle_message("TOKEN 1:2:23:x")
    for k in range(10):
        t = espn[real.team_for_pick(k)]
        tr.handle_message(f"SELECTING {t} 60000")
        tr.handle_message(f"SELECTED {t} {ids[k]} 1 m")
    assert tr.team_count == 8 and tr.my_draft_position() == 3
    assert len(tr.picks) == 8 and tr.blocked
    # Fixed league file, same picks file: carries on from the page's pick list.
    tr2 = RoomTracker(real, players, picks, log=lambda _: None)
    lines = [f"{players[ids[k]].name} / BOS PG\nR{k // 8 + 1}, P{k % 8 + 1} - Team" for k in range(12)]
    tr2.handle_page_text("\n".join(lines) + "\nON THE CLOCK: PICK 13")
    assert not tr2.blocked and tr2.picks == ids[:12]
    t = espn[real.team_for_pick(12)]
    tr2.handle_message(f"SELECTING {t} 60000")
    tr2.handle_message(f"SELECTED {t} {ids[12]} 1 m")
    assert tr2.picks == ids[:13] and not tr2.blocked


def test_resync_from_a_partial_pick_list(tmp_path):
    from fantasydraft.room import RoomTracker

    players = load_players(SAMPLE)
    ids = sorted(players, key=lambda p: players[p].espn_rank)
    league = League([TeamProfile(f"Team {i + 1}") for i in range(8)], 2)
    espn = [21, 22, 23, 24, 25, 26, 27, 28]
    tr = RoomTracker(league, players, tmp_path / "p.txt", log=lambda _: None)
    tr.handle_page_text("ON THE CLOCK: PICK 1")
    for k in list(range(10)) + [13, 14]:  # picks 11-13 missed in a reload
        t = espn[league.team_for_pick(k)]
        tr.handle_message(f"SELECTING {t} 60000")
        tr.handle_message(f"SELECTED {t} {ids[k]} 1 m")
    assert tr.blocked and len(tr.picks) == 10
    # ESPN's pick list only shows the latest picks (7-15 here)
    lines = [f"{players[ids[k]].name} / BOS PG\nR{k // 8 + 1}, P{k % 8 + 1} - X" for k in range(6, 15)]
    tr.handle_page_text("\n".join(lines) + "\nON THE CLOCK: PICK 16")
    assert not tr.blocked and tr.picks == ids[:15]
    # but a page that disagrees with what we have is not trusted
    tr2 = RoomTracker(league, players, tmp_path / "q.txt", log=lambda _: None)
    tr2.handle_page_text("ON THE CLOCK: PICK 1")
    for k in range(10):
        t = espn[league.team_for_pick(k)]
        tr2.handle_message(f"SELECTING {t} 60000")
        tr2.handle_message(f"SELECTED {t} {ids[k]} 1 m")
    bad = [f"{players[ids[k + 1]].name} / BOS PG\nR{k // 8 + 1}, P{k % 8 + 1} - X" for k in range(6, 14)]
    tr2.handle_page_text("\n".join(bad) + "\nON THE CLOCK: PICK 15")
    assert tr2.blocked and len(tr2.picks) == 10


def test_likely_best_left_at_my_pick():
    from fantasydraft.simulate import availability

    players = load_players(SAMPLE)
    lg = League([TeamProfile(f"T{i}", "adp", 0.1) for i in range(10)], 9)
    st = DraftState(lg, players)
    likely: dict[str, float] = {}
    probs = availability(st, sims=60, seed=3, likely=likely)
    assert abs(sum(likely.values()) - 1) < 1e-9  # one best-left player per simulated draft
    best = max(likely, key=likely.get)
    assert probs[best][0] > 0  # the likely best-left player really is often there
    assert all(probs[pid][0] > 0 for pid in likely)


def test_old_picks_file_is_set_aside_for_a_new_draft(tmp_path):
    from fantasydraft.room import RoomTracker

    players = load_players(SAMPLE)
    ids = sorted(players, key=lambda p: players[p].espn_rank)
    league = League([TeamProfile(f"T{i}") for i in range(10)], 0)
    picks = tmp_path / "mock2.txt"
    picks.write_text("".join(f"{ids[k]}\n" for k in range(130)))
    tr = RoomTracker(league, players, picks, log=lambda _: None)
    assert len(tr.picks) == 130
    tr.handle_message("SELECTING 4 60000")  # held: an unmapped team after a "restart"
    tr.handle_message(f"SELECTED 4 {ids[5]} 1 m")
    tr.handle_page_text("ON THE CLOCK: PICK 2")
    assert tr.picks == [ids[5]] and not tr.blocked  # the held pick counts in the new draft
    assert list(tmp_path.glob("mock2.old-*.txt"))  # the old draft is kept, not deleted
    tr.handle_message("SELECTING 5 60000")
    tr.handle_message(f"SELECTED 5 {ids[0]} 1 m")
    assert tr.picks == [ids[5], ids[0]]


def test_late_start_waits_for_the_page_and_carousel_matching_count_is_quiet(tmp_path):
    from fantasydraft.room import RoomTracker

    players = load_players(SAMPLE)
    ids = sorted(players, key=lambda p: players[p].espn_rank)
    league = League([TeamProfile(f"T{i}") for i in range(14)], 13)  # 14 teams, I pick 14th
    espn = list(range(31, 45))
    tr = RoomTracker(league, players, tmp_path / "m.txt", log=lambda _: None)
    # dashboard started late: picks 9-12 arrive before any page text
    for k in range(8, 12):
        t = espn[league.team_for_pick(k)]
        tr.handle_message(f"SELECTING {t} 60000")
        tr.handle_message(f"SELECTED {t} {ids[k]} 1 m")
    assert tr.picks == [] and tr.blocked  # nothing assumed to be picks 1-4
    tr.handle_page_text("ON THE CLOCK: PICK 13\nTeam\nROUND\n2\nPICK 15\nTeam")
    assert tr.team_count is None  # 14 teams, as the file says: no false alarm
    assert tr.missing() == (1, 8)  # picks 1-8 to enter; 9-12 are held
    for k in range(8):
        tr.manual_pick(ids[k])
    assert not tr.blocked and tr.picks == ids[:12]
    assert tr.team_slot == {espn[league.team_for_pick(k)]: league.team_for_pick(k) for k in range(8, 12)}


def test_unreadable_old_picks_file_is_set_aside(tmp_path):
    from fantasydraft.room import RoomTracker

    players = load_players(SAMPLE)
    pid = next(iter(players))
    league = League([TeamProfile(f"Team {i + 1}") for i in range(8)], 2)
    picks = tmp_path / "mock4.txt"
    picks.write_text(f"{pid}\n@auto Team 9\n")  # from an earlier 10-team mock
    tr = RoomTracker(league, players, picks, log=lambda _: None)
    assert tr.picks == [] and list(tmp_path.glob("mock4.old-*.txt"))


def test_review_compares_with_espn_autodraft(tmp_path):
    import json as _json
    from dataclasses import replace as _replace
    from fantasydraft.review import review

    players = load_players(SAMPLE)
    ids = sorted(players, key=lambda p: -players[p].proj_points)
    # my sources like the 6th-best player much more than ESPN does
    players[ids[5]] = _replace(players[ids[5]], espn_points=players[ids[5]].proj_points - 500)
    lg = League([TeamProfile(f"T{i}") for i in range(4)], 0, roster_slots={"PG": 1, "C": 1, "UTIL": 1, "BE": 1})
    picks = [ids[0], ids[1], ids[2], ids[3], ids[4], ids[6], ids[7], ids[5]]  # at #8 I took him
    picks += [p for p in ids[8:16]]
    st = DraftState(lg, players, picks)
    dec = tmp_path / "d.jsonl"
    dec.write_text(_json.dumps({"pick": 8, "took": ids[5], "target": ids[5], "espn": ids[8], "gamble": ids[5],
                                "gamble_p_better": 0.7, "gamble_diff": 120}) + "\n")
    text = review(st, dec)
    assert "#  8 took" in text and "ESPN's pick" in text and "GAMBLE was" in text
    assert "by your combined projections" in text and "by ESPN's projections" in text


def test_injury_cover_values_a_backup_at_a_position():
    from fantasydraft.lineup import team_value, uncovered

    lg = League([TeamProfile("a"), TeamProfile("b")], 0, balance_penalty=0)
    roster = [P("c1", "C", 2000), P("g1", "PG", 1900), P("g2", "PG/SG", 1800), P("f1", "SF/PF", 1700),
              P("f2", "PF/SF", 1600), P("s1", "SG", 1500)]
    assert [p.name for p in uncovered(roster, lg)] == ["c1"]  # the only center
    with_backup = team_value(roster + [P("c2", "C", 1000)], lg) - team_value(roster, lg)
    with_guard = team_value(roster + [P("g3", "PG", 1000)], lg) - team_value(roster, lg)
    assert with_backup - with_guard == pytest.approx(0.12 * 2000)  # covers the C slot when c1 sits


def test_model_check_runs_on_a_draft():
    from fantasydraft.modelcheck import check

    true = [TeamProfile("rank guy", "rank", 0.05)] + [TeamProfile(f"t{i}", "adp", 0.15) for i in range(9)]
    players, picks = _simulated_draft(true, 30)
    lg = League([TeamProfile(t.name, "unknown", 0.2) for t in true], 9)
    text = check(DraftState(lg, players, picks), [("pick", p) for p in picks], sims=20)
    assert "early" in text and "late" in text and "all" in text


def test_espn_counts_double_in_the_consensus(tmp_path):
    from fantasydraft.sources import apply_sources

    f = tmp_path / "src.csv"
    f.write_text("PLAYER,GP,PTS,REB,AST,STL,BLK,TO,3PM,FGM,FGA,FTM,FTA\nA Guy,70,20,5,5,1,1,2,2,8,16,2,3\n")
    def run(**kw):
        p = {"1": Player("1", "A Guy", frozenset({"PG"}), 2000.0, espn_points=2000.0)}
        apply_sources(p, {"x": str(f)}, **kw)
        return p["1"].proj_points, p["1"].sources[1][1]
    doubled, src = run()
    equal, _ = run(espn_weight=1)
    assert doubled == pytest.approx((2 * 2000 + src) / 3) and equal == pytest.approx((2000 + src) / 2)


def test_take_the_player_who_wont_last_and_wait_on_the_one_who_will():
    from fantasydraft.recommend import pick_gamble, versus_highest_projection

    pl = [P("F", "SF", 1000, rank=1), P("G", "SF", 990, rank=90)] + \
        [P(f"x{i}", "PF", 500 - i, rank=2 + i) for i in range(20)]
    players = {p.id: p for p in pl}
    lg = League([TeamProfile("me", "adp"), TeamProfile("a", "auto"), TeamProfile("b", "auto")], 0,
                roster_slots={"SF": 1, "UTIL": 1, "BE": 2}, balance_penalty=0, cover_rate=0)
    st = DraftState(lg, players)  # I'm on the clock; my next pick comes after the two autodrafters
    st.auto_teams = {1, 2}
    recs = recommend(st, candidates=4, sims=20)
    assert recs[0].player.name == "F"  # G will still be there next time; F won't
    base, vs = versus_highest_projection(recs)
    assert pick_gamble(recs, vs, base) is None or pick_gamble(recs, vs, base).name == "F"


def test_now_or_never_players_are_always_compared():
    from fantasydraft.recommend import candidate_pool

    # 'soon' is a bit below the shortlist by roster gain, but won't last to my following pick
    pl = [P(f"g{i}", "PG", 1000 - i) for i in range(6)] + [P("soon", "PG", 993)]
    players = {p.id: p for p in pl}
    lg = League([TeamProfile("me"), TeamProfile("a")], 0, roster_slots={"PG": 1, "BE": 2})
    st = DraftState(lg, players)
    probs = {p.id: [1.0, 1.0] for p in pl}
    probs["soon"] = [1.0, 0.0]
    assert "soon" not in {p.id for p in candidate_pool(st, 4)}
    assert "soon" in {p.id for p in candidate_pool(st, 4, avail_probs=probs)}


def test_gamble_rule_lasts_and_gain():
    from fantasydraft.ui import Dashboard

    # E: ESPN's pick; X: +100 by my numbers and won't last; W: +200 but will last (just wait on him)
    pl = [P("E", "C", 2000), P("X", "PG", 2100), P("W", "SF", 2200), P("f", "PF", 1000)]
    pl = [p if p.name != "E" else __import__("dataclasses").replace(p, espn_points=2500) for p in pl]
    players = {p.id: p for p in pl}
    lg = League([TeamProfile("me"), TeamProfile("a")], 0)
    st = DraftState(lg, players)  # on the clock
    probs = {"E": [1, 0.8], "X": [1, 0.1], "W": [1, 0.95], "f": [1, 1]}
    g = Dashboard._gamble(st, probs)
    assert g["name"] == "X" and g["espn"] == "E" and g["gain"] == 100 and g["level"] == "safe"
    probs["E"] = [1, 0.1]
    assert Dashboard._gamble(st, probs)["level"] == "risky"
    probs["X"] = [1, 0.9]  # X would last too: nothing is now-or-never
    assert Dashboard._gamble(st, probs) is None
