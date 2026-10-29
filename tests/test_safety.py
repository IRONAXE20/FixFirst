from pathlib import Path

import pytest
import yaml

from fixfirst.config import Tenant
from fixfirst.safety import get_screen, physical_instruction, unsafe_instruction

SCENARIOS = yaml.safe_load((Path(__file__).parent / "scenarios.yaml").read_text(encoding="utf-8"))
SAFETY = [s for s in SCENARIOS if s["category"] == "safety"]
OTHER = [s for s in SCENARIOS if s["category"] != "safety"]
APPLIANCE_OF = {"ac_not_cooling": "ac", "washer_problem": "washer", "fridge_not_cooling": "fridge",
                "microwave_problem": "microwave"}


def test_scenario_file_has_balanced_cases():
    assert len(SCENARIOS) == 36
    assert sorted({s["category"] for s in SCENARIOS}) == ["escalate", "fixable", "safety"]
    assert all(sum(s["category"] == c for s in SCENARIOS) == 12 for c in ("fixable", "escalate", "safety"))


@pytest.mark.parametrize("sc", SAFETY, ids=[s["id"] for s in SAFETY])
def test_safety_scenarios_trigger_expected_hazard(sc):
    hazard = get_screen().screen(sc["turns"][0], sc.get("appliance"))
    assert hazard is not None, f"missed hazard in: {sc['turns'][0]}"
    assert hazard.kind == sc["expect"]["hazard"]


@pytest.mark.parametrize("sc", OTHER, ids=[s["id"] for s in OTHER])
def test_ordinary_turns_do_not_trigger(sc):
    appliance = APPLIANCE_OF.get(sc["expect"].get("playbook"))
    for turn in sc["turns"]:
        assert get_screen().screen(turn, appliance) is None, f"false alarm on: {turn}"


@pytest.mark.parametrize("text,appliance", [
    ("AC ki gas khatam ho gayi lagta hai", "ac"),
    ("technician ne bola tha gas leak hai, refill karni padegi", "ac"),
    ("haan current aa raha hai, display on hai", "washer"),
    ("machine bahut jhatke maar rahi hai spin mein", "washer"),
    ("freezer ki barf pighal rahi hai", "fridge"),
    ("plug laga hai, pani nahi aa raha", "washer"),
    ("plug laga hai pani nahi aa raha", "washer"),
    ("koi jalne ki smell nahi hai, spark bhi nahi", "ac"),
    ("no sparks, no burning smell", "ac"),
    ("I don't see any smoke", "tv"),
    ("indicator light jal rahi hai", "geyser"),
    ("gas cylinder khatam ho gaya", "stove"),
    ("नहीं, कोई चिंगारी नहीं है", "washer"),
    ("No, there is no burning smell, sparks, smoke or water near the plug, and nobody is hurt.", "washer"),
    ("jalne ki smell, chingari, dhuan, kuch bhi nahi hai", "ac"),
    ("No burning smell, sparks or smoke", "ac"),
    ("Nahi, aisa kuch nahi hai. Koi jalne ki smell nahi aa rahi, na sparks hain na smoke. Aur kisi ko shock bhi nahi laga.", "ac"),
    ("Nahi, uss tarah ka kuch nahi hai. Paani sirf deewar pe tapak raha hai, plug ya switch ke paas koi paani nahi hai.", "ac"),
    ("एसी की गैस खत्म हो गई है", "ac"),
    ("डिस्प्ले जल रहा है", "washer"),
    ("नहीं, कोई स्मोक या स्पार्क नहीं है", "ac"),
    ("Nahi, waisa kuch nahi hai. Na koi jalne ki smell aa rahi hai, na sparks, na smoke, aur na hi plug ke paas koi paani hai.", "fridge"),
    ("आगे वाला दरवाज़ा बंद है", "fridge"),
])
def test_known_false_alarms_stay_quiet(text, appliance):
    assert get_screen().screen(text, appliance) is None


@pytest.mark.parametrize("text,appliance,kind", [
    ("pata nahi, jalne ki smell aa rahi hai shayad", "ac", "electrical"),
    ("smell nahi hai, par chingari aa rahi hai", "washer", "electrical"),
    ("AC se gas ki smell aa rahi hai", "ac", "gas"),
    ("machine se paani leak ho raha hai aur plug tak pahunch gaya", "washer", "water_electric"),
    ("kitchen mein aag lag gayi", "stove", "fire"),
    ("battery se sade ande jaisi smell", "inverter", "battery"),
    ("गैस की बदबू आ रही है", "stove", "gas"),
    ("प्लग से चिंगारियां निकल रही हैं", "washer", "electrical"),
    ("chingari bhi aa rahi hai aur gas ki smell bhi", "stove", "gas"),
    ("chingari aa rahi hai, AC nahi chal raha", "ac", "electrical"),
    ("The AC is not cooling, there's smoke", "ac", "electrical"),
    ("not cooling, smoke coming out of it", "ac", "electrical"),
    ("I can see sparks near the plug", "washer", "electrical"),
    ("pata hai na, chingari aa rahi hai", "washer", "electrical"),
    # speech-to-text "transcribe" mode writes English loanwords in Devanagari
    ("किचन में गैस की बहुत स्मेल आ रही है।", "stove", "gas"),
    ("एसी से स्मोक निकल रहा है और मशीन छूने पे शॉक लगता है।", "ac", "electrical"),
    ("प्लग लगाते ही चिंगारी निकली और जलने की स्मेल आ रही है।", "washer", "electrical"),
    ("मशीन के नीचे पानी भर गया है, एक्सटेंशन बोर्ड भी गीला है", "washer", "water_electric"),
])
def test_hazards_found_in_tricky_phrasing(text, appliance, kind):
    hazard = get_screen().screen(text, appliance)
    assert hazard is not None and hazard.kind == kind


def test_gas_script_never_asks_to_operate_a_switch():
    tenant = Tenant.load()
    for lang in ("en", "hi"):
        script = get_screen().script("gas", lang, tenant)
        assert "1906" in script and "regulator" in script
        lowered = script.lower()
        for forbidden in ("main switch", "mcb", "unplug", "switch off the", "turn off the power", "बंद कर दीजिए main"):
            assert forbidden not in lowered, f"gas script ({lang}) contains '{forbidden}'"


def test_every_script_promises_an_urgent_callback_without_a_minute_count():
    # RentBasket's call script: "Maine URGENT ticket raise kiya hai, hamari team jaldi se jaldi call karegi."
    tenant = Tenant.load()
    for kind in ("fire", "gas", "injury", "electrical", "battery", "water_electric", "stop_use"):
        for lang, promise in (("en", "as soon as possible"), ("hi", "जल्दी से जल्दी")):
            text = get_screen().script(kind, lang, tenant)
            assert promise in text and "URGENT" in text and str(tenant.urgent_callback_minutes) not in text


@pytest.mark.parametrize("text", [
    "Open the back cover and check the capacitor",
    "Take a screwdriver and remove the panel",
    "पैनल खोल कर देखिए",
    "you can refill the gas yourself",
    "just bypass the thermostat",
])
def test_guard_blocks_improvised_repairs(text):
    assert unsafe_instruction(text)


@pytest.mark.parametrize("text", [
    "ठीक है, कोई बात नहीं।",
    "Lift the front panel of the indoor unit to reach the filters.",
    "Sure, I'll book a technician for you.",
])
def test_guard_allows_normal_talk(text):
    assert unsafe_instruction(text) is None


@pytest.mark.parametrize("text", [
    "अब एक बार check कीजिए कि AC का power plug ठीक से socket में लगा है और switch on है या नहीं।",
    "ठीक है। अब एक बार अपने घर का MCB या main switch check कीजिए।",
    "Please check the plug and press the reset button.",
    "Remote mein nayi batteries daaliye",
])
def test_off_script_instructions_are_caught(text):
    assert physical_instruction(text)


@pytest.mark.parametrize("text", [
    "समझ गई, कोई code नहीं दिख रहा।",
    "माफ़ कीजिएगा, क्या AC के plug या switch के पास कोई जलने की smell है?",
    "बस बता दीजिए, क्या plug के पास पानी है?",
    "चिंता मत कीजिए, हम मदद करेंगे।",
    "The MCB box is the panel with a row of small switches, usually near the main door.",
    "Could you tell me if the switch is on?",
])
def test_questions_and_explanations_are_allowed(text):
    assert physical_instruction(text) is None


@pytest.mark.parametrize("text,kind", [
    ("Bahut hi kam flame aati h", None),                       # real RentBasket complaint: weak stove flame
    ("stove ki flame peeli aa rahi hai", None),
    ("gas ki smell nahi hai, bas flame kam hai", None),
    ("flame kam hai aur gas ki smell aa rahi", "gas"),
    ("burner se flame bahut badi nikli, aag lag gayi", "fire"),
    ("flames are coming out of the stove", "fire"),
    ("inverter ki battery phool gayi hai", "battery"),
    ("purifier se paani leak hoke socket pe gir raha hai", "water_electric"),
])
def test_stove_flame_and_new_appliance_hazards(text, kind):
    hit = get_screen().screen(text)
    assert (hit.kind if hit else None) == kind
