import pytest

from fixfirst.playbooks import PlaybookError, PlaybookRun, get_library


def test_all_playbooks_load_and_validate():
    lib = get_library()
    assert {pb.appliance for pb in lib.playbooks.values()} == {"ac", "washer", "washer_semi", "fridge", "inverter", "tv", "water_purifier", "stove"}
    assert lib.errors == []


def test_every_step_is_bilingual_and_terminals_are_reachable():
    for pb in get_library().playbooks.values():
        kinds = {n.kind for n in pb.nodes.values()}
        assert {"resolve", "escalate"} <= kinds, pb.id
        for n in pb.nodes.values():
            if n.say:
                assert n.text("en") and n.text("hi"), (pb.id, n.id)


def test_symptom_skips_triage_after_safety_check():
    run = PlaybookRun(get_library().for_appliance("ac"))
    assert run.start("not_cooling").id == "safety_check"
    assert run.advance("no_hazard").node.id == "mode_check"
    assert [p["node"] for p in run.path] == ["safety_check", "triage"]


def test_happy_path_reaches_resolution():
    run = PlaybookRun(get_library().for_appliance("ac"))
    run.start("not_starting")
    run.advance("no_hazard")
    assert run.node.id == "remote_display"
    run.advance("remote_blank")
    result = run.advance("fixed")
    assert result.node.kind == "resolve" and run.finished


def test_invalid_outcome_is_rejected():
    run = PlaybookRun(get_library().for_appliance("washer"))
    run.start()
    with pytest.raises(PlaybookError):
        run.advance("open_the_back_panel")


def test_hazard_outcome_stops_the_playbook():
    run = PlaybookRun(get_library().for_appliance("washer"))
    run.start("not_filling")
    result = run.advance("hazard_water")
    assert result.hazard == "water_electric" and result.node is None


def test_routes_only_to_declared_nodes():
    run = PlaybookRun(get_library().for_appliance("washer"))
    run.start("error_code")
    run.advance("no_hazard")
    assert run.jump("not_filling", "LG IE").id == "nf_tap"
    with pytest.raises(PlaybookError):
        run.jump("anything_else", "nope")
    with pytest.raises(PlaybookError):
        run.jump_to("nf_hose", "not terminal")


@pytest.mark.parametrize("brand,code,route", [
    ("LG", "IE", "not_filling"),
    ("lg", "o e", "not_draining"),
    ("LG", "0E", "not_draining"),
    ("एलजी", "UE", "unbalanced"),
    ("LG", "tcL", "tub_clean"),
    ("Samsung", "4 C", "not_filling"),
    ("samsung", "5E", "not_draining"),
    ("Samsung", "dC", "lid"),
    ("LG", "PE", "restart_then_escalate"),
])
def test_documented_codes_route(brand, code, route):
    hit = get_library().lookup_code("washer", brand, code)
    assert hit and hit["route"] == route


@pytest.mark.parametrize("brand,code", [("Whirlpool", "F05"), ("LG", "ZZ"), ("Voltas", "E4")])
def test_unknown_codes_are_not_guessed(brand, code):
    assert get_library().lookup_code("washer", brand, code) is None


def test_ac_codes_are_never_interpreted():
    assert get_library().lookup_code("ac", "LG", "CH38") is None
