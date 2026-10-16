import json

import pytest

from setlistarr.core.plex.sync import sync_all
from setlistarr.generation.constraints import Constraints
from setlistarr.generation.draft import DraftState, norm_artist, norm_title
from setlistarr.generation.runner import GenerationManager
from setlistarr.generation.two_stage import map_tags
from setlistarr.generation.validate import finalize
from setlistarr.llm.registry import ProviderConfig, save_provider
from setlistarr.services.generations import get_generation, load_draft

from .fakes.scripted_provider import ScriptedProvider, call, say, tools

# fake library: 6 tracks x 200s. Slowdive 100,101 (shoegaze); Pixies 102-105.


@pytest.fixture
def lib(conn, plex):
    sync_all(conn, plex, full=True)
    save_provider(conn, ProviderConfig(id=None, name="Claude", kind="anthropic", model="claude-opus-5-5"))
    return conn


def run(conn, steps, prompt="dreamy shoegaze", constraints=None, mode=None):
    provider = ScriptedProvider(steps)
    gen_id = GenerationManager().start(
        conn, prompt, constraints=constraints, mode=mode, background=False, inject={"provider": provider}
    )
    return get_generation(conn, gen_id), provider


def test_tool_agent_grounds_every_track(lib):
    c = Constraints.from_form({"target_minutes": 10})  # window 7-13 min -> at most 3 tracks of 200s
    g, provider = run(lib, [
        tools(call("list_facets")),
        tools(call("search_tracks", any_tags=["shoegaze"])),
        tools(call("add_to_draft", plex_keys=["100", "999999", "101"]), text="Starting dreamy."),
        tools(call("set_constraints", target_minutes=90, max_per_artist=1)),  # target locked, cap not
        tools(call("add_to_draft", plex_keys=["102", "103"])),
        tools(call("finish", title="Haze", reasoning="Shoegaze first.")),
    ], constraints=c)

    assert g["status"] == "ok", g["error"]
    assert g["mode_used"] == "tools" and g["title"] == "Haze" and g["llm_calls"] == 6
    d = load_draft(lib, g["draft_id"])
    # 999999 invented -> rejected at add time. 100 and 101 went in before the model lowered the
    # cap to 1; 103 was rejected live; final re-validation then drops 101.
    assert [t["plex_key"] for t in d["tracks"]] == ["100", "102"]
    assert any("max per artist" in a for a in d["adjustments"])
    assert any("short of the target" in a for a in d["adjustments"])  # 6.7 min < 7 min window
    # the model was told about the invented key
    first_add = json.loads(provider.requests[3]["messages"][-1]["content"])
    assert first_add["rejected"] == [{"plex_key": "999999", "reason": "not a track in this library (unknown or removed plex_key)"}]
    # locked target survived the model's set_constraints
    assert g["constraints"]["target_minutes"] == 10 and g["constraints"]["max_per_artist"] == 1
    # cost from the seeded claude-opus-5-5 pricing: 6 calls x (1000 in @ $4 + 100 out @ $20) / 1M
    assert g["cost_usd"] == pytest.approx(6 * (1000 * 4 + 100 * 20) / 1e6)
    # create_plex_playlist is never offered to the agent
    assert "create_plex_playlist" not in {t.name for t in provider.requests[0]["tools"]}


def test_auto_mode_falls_back_to_two_stage(lib):
    g, provider = run(lib, [
        say("Here are some great songs: Alison, Debaser..."),  # no tool calls
        say("Sure!"),                                            # still none -> fallback
        say('```json\n{"any_tags": ["shoegazing", "not-a-tag"], "all_tags": [], "exclude_tags": [],'
            ' "artists": [], "exclude_artists": [], "year_min": null, "year_max": null, "min_rating": null,'
            ' "played": "any", "target_minutes": 10, "max_per_artist": 2, "mood": "dreamy"}\n```'),
        lambda req: say(json.dumps({"title": "Dream", "reasoning": "r", "order": [2, 1, 999, "x"]})),
    ])
    assert g["status"] == "ok", g["error"]
    assert g["mode_used"] == "two_stage"
    d = load_draft(lib, g["draft_id"])
    adj = " | ".join(d["adjustments"])
    assert "Fell back to two-stage" in adj and "not-a-tag" in adj and "Ignored 2 pick(s)" in adj
    # stage 2 only ever saw numbered candidates, and every final key is real
    assert all(not t["missing"] for t in d["tracks"]) and d["tracks"]
    assert provider.requests[2]["json_schema"]["required"]  # criteria schema was sent


def test_tools_mode_does_not_fall_back(lib):
    g, _ = run(lib, [say("no"), say("still no")], mode="tools")
    assert g["status"] == "error" and "Tool-calling mode failed" in g["error"]


def test_two_stage_unparseable_selection_uses_ranked_shortlist(lib):
    crit = json.dumps({"any_tags": ["rock"], "played": "any", "target_minutes": 10})
    g, _ = run(lib, [say(crit), say("I like these!"), say("nope")], mode="two_stage")
    assert g["status"] == "ok"
    assert any("ranked shortlist" in a for a in load_draft(lib, g["draft_id"])["adjustments"])


def test_finalize_trims_and_backfills(lib):
    c = Constraints(target_minutes=10, max_per_artist=5)  # 7-13 min window
    over = finalize(lib, ["100", "101", "102", "103", "104", "105"], c, pool=[])
    assert len(over.tracks) == 3 and "Trimmed 3" in over.adjustments[0]

    short = finalize(lib, ["100"], c, pool=["100", "bogus", "102", "103"])
    assert [t["plex_key"] for t in short.tracks] == ["100", "102", "103"]
    assert [t["source"] for t in short.tracks] == ["model", "backfill", "backfill"]

    starved = finalize(lib, ["100"], Constraints(target_minutes=120), pool=[])
    assert "short of the target" in starved.adjustments[-1]


def test_draft_rejects_same_song_other_version_and_exclusions(lib):
    lib.execute(
        "INSERT INTO tracks(plex_key, title, artist_name, duration_ms, section_id) "
        "VALUES('200', 'Alison (2005 Remaster)', 'Slowdive', 200000, 1)"
    )
    lib.commit()
    ds = DraftState(lib, Constraints(target_minutes=60, exclude_artists=["pixies"]))
    res = ds.add(["100", "200", "102"])
    assert res["accepted"] == ["100"]
    assert {r["reason"] for r in res["rejected"]} == {
        "same song is already in the draft (another version/album)", "artist is excluded"}


def test_normalizers():
    assert norm_title("Song (2011 Remaster)") == norm_title("Song - Live at Leeds") == norm_title("song [mono]")
    assert norm_artist("Pixies feat. Guest") == norm_artist("pixies")


def test_map_tags():
    vocab = {"shoegaze": 10, "dream pop": 5, "rock": 50, "alternative rock": 20}
    mapped, dropped = map_tags(["Shoegazing", "dreampop", "polka"], vocab)
    assert mapped == ["shoegaze", "dream pop"] and dropped == ["polka"]


def test_no_provider_configured(conn):
    from setlistarr.llm.base import ProviderError

    with pytest.raises(ProviderError, match="No AI provider"):
        GenerationManager().start(conn, "anything", background=False)


def test_auto_gives_up_on_tools_when_draft_stays_empty(lib):
    exploring = [tools(call("list_facets")) for _ in range(20)]
    crit = json.dumps({"any_tags": ["rock"], "played": "any", "target_minutes": 10})
    g, _ = run(lib, exploring + [say(crit), say(json.dumps({"title": "t", "reasoning": "r", "order": [1, 2, 3]}))])
    assert g["status"] == "ok" and g["mode_used"] == "two_stage"
    assert any("nothing added to the draft after 20 steps" in a for a in load_draft(lib, g["draft_id"])["adjustments"])


def test_moods_rank_but_do_not_filter(lib):
    from setlistarr.core.tools import ToolContext, search_tracks
    from setlistarr.generation.two_stage import split_genre_mood

    assert split_genre_mood(lib, ["shoegaze", "energetic", "rock"]) == (["shoegaze", "rock"], ["energetic"])
    res = search_tracks(ToolContext(conn=lib), any_tags=["shoegaze"], boost_tags=["melancholy"])
    assert [t["title"] for t in res["tracks"]][0] == "Alison"  # only shoegaze track with the mood
    assert {t["title"] for t in res["tracks"]} == {"Alison", "Machine Gun"}  # mood didn't widen it
