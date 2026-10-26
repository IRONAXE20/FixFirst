import pytest

from fixfirst.playbooks import PlaybookError, PlaybookRun, get_library


def test_all_playbooks_load_and_validate():
    lib = get_library()
    assert {pb.appliance for pb in lib.playbooks.values()} == {"ac", "washer", "fridge", "microwave"}
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
    assert run.advance("no_hazard").node.id == "ac01_air"
    assert [p["node"] for p in run.path] == ["safety_check", "triage"]


def test_happy_path_reaches_resolution():
    run = PlaybookRun(get_library().for_appliance("ac"))
    run.start("not_starting")
    run.advance("no_hazard")
    assert run.node.id == "ac07_remote"
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
    with pytest.raises(PlaybookError):
        run.jump("not_filling", "no code routes: RentBasket's SOP never interprets codes")
    with pytest.raises(PlaybookError):
        run.jump_to("wm02_hose", "not terminal")
    assert run.jump_to("esc_error_code", "code F05").kind == "escalate"


@pytest.mark.parametrize("appliance,brand,code", [
    ("washer", "Whirlpool", "F05"), ("washer", "LG", "IE"), ("ac", "Voltas", "E4"), ("ac", "Lloyd", "E1"),
    ("fridge", "Whirlpool", "E1"), ("microwave", "Panasonic", "H97"),
])
def test_error_codes_are_never_interpreted(appliance, brand, code):
    # RentBasket SOP WM-11 / MW-10: never infer a code from another model or brand; record it for the technician.
    assert get_library().lookup_code(appliance, brand, code) is None
    assert "esc_error_code" in get_library().for_appliance(appliance).nodes


def test_playbooks_follow_the_sop_boundaries():
    """SOP 3.2: nothing at height, no switchboards or MCB resets, no drain/pump/inlet filters, no tightening,
    no unplugged defrost, no feet adjusting. Checked on every spoken English line."""
    banned = ["stool to", "ladder to", "climb up to", "mcb box", "push that switch up", "open the filter cover",
              "unscrew", "tighten the fitting", "unplug the fridge", "levelling feet", "pour", "toothpick", "hair dryer on"]
    for pb in get_library().playbooks.values():
        assert pb.status == "draft-unreviewed"   # wording still needs RentBasket's sign-off
        for n in pb.nodes.values():
            text = n.text("en").lower()
            for phrase in banned:
                assert phrase not in text, (pb.id, n.id, phrase)


def test_every_escalation_names_its_sop_playbook_and_issue_code():
    import re
    for pb in get_library().playbooks.values():
        for n in pb.nodes.values():
            if n.kind == "escalate" and n.id != "esc_error_code":
                assert re.match(r"(AC|FR|WM|MW)-\d\d [A-Z_]+ \(P[0-3]\): ", n.likely_cause), (pb.id, n.id)


def test_stop_use_faults_are_hazards():
    mw = get_library().for_appliance("microwave")
    assert mw.node("safety_check").outcomes["hazard_door"].hazard == "stop_use"
    assert get_library().for_appliance("washer").node("safety_check").outcomes["hazard_violent"].hazard == "stop_use"
