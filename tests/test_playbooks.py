import re

import pytest

from fixfirst.playbooks import PlaybookError, PlaybookRun, get_library

from conftest import SOP


def test_all_playbooks_load_and_validate():
    lib = get_library()
    assert {pb.appliance for pb in lib.playbooks.values()} == {"ac", "washer", "fridge", "microwave", "water_purifier",
                                                               "furniture"}
    assert SOP.errors == []   # the archived SOP set still validates (the engine tests run on it)
    assert lib.errors == []


def test_every_step_is_bilingual_and_terminals_are_reachable():
    for pb in get_library().playbooks.values():
        kinds = {n.kind for n in pb.nodes.values()}
        assert "escalate" in kinds, pb.id
        for n in pb.nodes.values():
            if n.say:
                assert n.text("en") and n.text("hi"), (pb.id, n.id)


def test_kb_ticket_issues_go_straight_to_a_ticket():
    run = PlaybookRun(get_library().for_appliance("fridge"))
    assert run.start("not_cooling").kind == "escalate" and run.finished
    assert [p["node"] for p in run.path] == ["triage"]


def test_kb_self_fix_reaches_resolution():
    run = PlaybookRun(get_library().for_appliance("washer"))
    assert run.start("not_filling").id == "wm01_off"
    for outcome in ("done", "done", "done"):
        run.advance(outcome)
    assert run.advance("fixed").node.kind == "resolve" and run.finished


def test_only_the_kb_self_fixes_have_steps():
    # KB: AC-04, FR-04, WM-01 and MW-04 are SELF-FIX; AC-03 asks its safety questions; everything else is a ticket.
    stepped = {}
    for pb in get_library().playbooks.values():
        tri = pb.node(pb.triage_node)
        stepped[pb.appliance] = sorted(o.id for o in tri.outcomes.values()
                                       if o.next and not pb.node(o.next).terminal)
    assert stepped == {"ac": ["power", "weak_airflow"], "fridge": ["leak_front"], "washer": ["leak", "not_filling"],
                       "microwave": ["bad_smell"], "water_purifier": [], "furniture": []}


def test_invalid_outcome_is_rejected():
    run = PlaybookRun(get_library().for_appliance("washer"))
    run.start("not_filling")
    with pytest.raises(PlaybookError):
        run.advance("open_the_back_panel")


def test_hazard_outcome_stops_the_playbook():
    run = PlaybookRun(get_library().for_appliance("washer"))
    run.start("leak")
    result = run.advance("near_plug")
    assert result.hazard == "water_electric" and result.node is None


def test_kb_safety_cases_are_hazards():
    mw = get_library().for_appliance("microwave")
    assert mw.node("triage").outcomes["sparking_noise"].hazard == "stop_use"   # MW-01
    assert mw.node("mw04_kind").outcomes["burning"].hazard == "stop_use"        # "treat it as MW-01"
    ac = get_library().for_appliance("ac")
    assert ac.node("ac03_socket").outcomes["burnt"].hazard == "electrical"      # AC-03


def test_only_terminal_jumps_are_allowed():
    run = PlaybookRun(get_library().for_appliance("washer"))
    run.start("not_filling")
    with pytest.raises(PlaybookError):
        run.jump("not_filling", "no routes in the KB playbooks")
    with pytest.raises(PlaybookError):
        run.jump_to("wm01_filter", "not terminal")
    assert run.jump_to("esc_wm01", "caller asked").kind == "escalate"


@pytest.mark.parametrize("appliance,brand,code", [
    ("washer", "Whirlpool", "F05"), ("washer", "LG", "IE"), ("ac", "Voltas", "E4"), ("fridge", "Whirlpool", "E1"),
    ("microwave", "Panasonic", "H97"),
])
def test_error_codes_are_never_interpreted(appliance, brand, code):
    assert get_library().lookup_code(appliance, brand, code) is None


def test_every_ticket_names_its_kb_issue():
    for pb in get_library().playbooks.values():
        for n in pb.nodes.values():
            if n.kind == "escalate":
                assert re.match(r"(AC|FR|WM|MW|WP|SF|BD|DT)-\d\d ", n.likely_cause) or "not in the RentBasket KB" in n.likely_cause, (pb.id, n.id)
        assert "other" in pb.node(pb.triage_node).outcomes, pb.id   # an issue the KB doesn't list still gets a ticket
