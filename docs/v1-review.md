# FixFirst v1: review notes

What was built, why it looks the way it does, and what is needed from RentBasket before real customers use it.

## Decisions you made (inputs)
- Browser call first, built so a phone number can be added later
- Sarvam end-to-end: speech-to-text, LLM and text-to-speech
- Manuals: public sources now, RentBasket SOPs later
- Customers only, with a WhatsApp handoff to the technician; RentBasket first, settings kept in config

## What the research turned up
- **Urban Company reel:** Raghav Chandra (co-founder) talking about AI as a *copilot for technicians* who diagnose in the field. That's the technician side; FixFirst is the customer side. The two meet in the handoff summary.
- **RentBasket (rentbasket.com):**
  - Serves Gurgaon and Noida; rents fridges, fully automatic washers, split/window ACs, LED TVs, microwaves, RO purifiers, inverters and gas stoves. No geysers are listed.
  - Maintenance is free, and requests come in through the app or the support line.
  - The FAQ names "AC not cooling" and "washing machine error code" as covered issues.
  - Listings show no brands or models.
- **Sarvam (October 2026):**
  - `sarvam-m` and `sarvam-30b` are deprecated. The voice-agent model is `sarvam-105b-conversations` (32K context, OpenAI-style tool calling, ₹15/₹60 per 1M input/output tokens).
  - Speech-to-text is `saaras:v4` (₹30/hour); text-to-speech is `bulbul:v3` (₹30 per 10K characters).
  - Pipecat 1.12 has Sarvam services and an Exotel serializer.
- **Error codes:** meanings vary by brand and model, and third-party lists contradict each other. Only official LG India and Samsung India washer codes are used. AC codes are recorded, never interpreted.

## What the council changed (council/council-report-*.html)
1. **The gas script was dangerous.** "Switch off at the mains" during a gas leak means flipping a switch, which can spark. Each hazard now has its own script, and the gas one never mentions a switch.
2. **Grounding is enforced by the system, not just requested in the prompt.** Playbooks are decision trees; the LLM only picks an outcome; scripts are spoken word for word; the server rejects anything else.
3. **The keyword screen alone misses garbled Hinglish.** There are now two screens, a fuzzy lexicon with spelling variants plus the LLM's hazard flag, and either one escalates.
4. **Customers have no reason to self-fix when repairs are free.** The opening line sells speed ("most problems fixed in a few minutes") and says "say technician anytime".
5. **Latency could kill it.** Every turn is timed (agent, LLM, STT, TTS, voice-to-voice) and shown on `/ops`.
6. **"Urgent escalation" needs a real destination.** Safety scripts give 112 / 101 / LPG 1906 and promise a callback within `urgent_callback_minutes`, and the on-call owner is configurable.

Where I went against the council, because your brief asks for it (each is easy to remove):
- **Document library with upload and search kept.** It backs the citations; the agent's doc-lookup tool ships switched off.
- **Fridge and geyser kept as starter playbooks**, next to the full AC and washer ones.
- **Error codes:** washer codes from official sources are acted on (IE/4C → water supply steps, OE/5C → drain steps, UE → rebalance, CL → child lock). Unknown codes and brands go to the technician.
- **`/ws/exotel` built:** it's about 20 lines with Pipecat and you chose "phone-ready". It has not been tested on a live number.

Also added beyond the brief:
- WhatsApp follow-ups: "still working?" 24 hours after a phone fix, a check-in after a "later" step, and a "we got cut off" message after a drop.
- A structured technician handoff.
- An offline demo brain so the UI works before the key arrives.

## Asks for RentBasket (needed before a pilot)
1. **Sarvam key.** Then run `python -m fixfirst eval` and record 20 real complaints from the support team to check Hinglish accuracy and latency.
2. **The last 90 days of service tickets, tagged by what the technician actually did.** This decides the product. If 15% or more are "nothing broken" (remote batteries, mode, MCB, tap, child lock, unbalanced load, filters), fixing by phone pays. If it's lower, lead with the technician handoff (first-visit fix rate).
3. **Fleet model list per appliance.** Playbooks can then be specific to each model, and the agent stops asking for brands.
4. **Sign-off by a senior technician and legal** on every playbook step and safety script (`status: reviewed`), plus a rental-agreement clause on what customers may safely do themselves.
5. **A named on-call owner and an after-hours response time** for urgent tickets (`knowledge/tenant.yaml`).
6. **WhatsApp Business:** the token, two approved templates, and team numbers.
7. **DPDP Act:** consent wording for recordings, transcripts and in-home photos, plus a retention policy. The greeting currently gives a recording notice only.
8. **Cost of one technician visit** (₹). The ops page then shows savings in rupees.

## Live test with the Sarvam key (2026-10-19)

**Bugs the eval found, now fixed (158 tests pass):**
- **Crash:** washer outcomes named `yes`/`no` were read by YAML as true/false, so any call reaching the child-lock question crashed. Renamed, and the loader now rejects such names.
- **Empty replies:** `sarvam-105b-conversations` often returned neither text nor a tool call (4 of 6 tries on one Hinglish turn). Now the model must always call a tool (plain replies go through a new `say` tool), with one retry. In testing this dropped to 0 of 12, for about 30 ms extra.
- **"Send a technician" wasn't honoured:** the model sometimes ignored it. Explicit requests ("send a technician", "koi aadmi bhej do", "टेक्नीशियन भेज दीजिए") now book directly without asking the model, while "technician ne bola tha…" and "agar theek nahi hua to…" don't trigger it.
- **"No hazard" was assumed:** the model answered the safety question for callers who hadn't. The server now only accepts the no-hazard answer when the caller actually said no, and stops asking after two tries.
- **Instructions in the model's own words:** when stuck, the model told callers to "check the plug / MCB" in its own words. The guard now replaces any appliance instruction outside the playbook with the current playbook step.
- **Safety-screen false alarms:**
  - Comma lists lost their "no" ("No burning smell, sparks, smoke…"). The "no" now carries across a list but stops at an affirming verb, so "chingari aa rahi hai, AC nahi chal raha" is still a spark report.
  - Hindi "na… na…" (neither/nor) wasn't understood as a negative.
  - The model flagged "light jal rahi hai" (lit) and "power trips when the AC starts" as emergencies. It now gets clearer guidance, and a hazard flagged only by the model, with no danger word from the caller, gets one safety question first.
- **Voice safety gap:** in speech-to-text "transcribe" mode, Sarvam writes English words in Devanagari ("स्मोक", "शॉक", "गैस की स्मेल"), and the screen missed a gas smell and a shock. The default is now `codemix` (English words stay in Roman script, codes arrive as "IE"/"4C"), and the lexicon also knows the Devanagari spellings.
- **Rate limits:** the eval hit Sarvam 429s at about 3–4 requests a second. Live calls now retry once, and the eval is paced to about 50 requests a minute.

**Scorecard history** (`python -m fixfirst eval`; 10 fixable / 10 escalate / 10 safety):

| Run | Fixable | Escalate | Safety | Notes |
|---|---|---|---|---|
| First, fixed caller lines | 7 | 2 | 10 | Crash on `yes`/`no` outcomes; many failures were the fixed lines not matching the agent |
| Simulated caller, after fixes | 9 | 6 | 10 | Remaining failures were Sarvam 429 rate limits |
| Paced | 9 | 8 | 10 | Comma-list and "na…na" false alarms; model-only hazard flag |
| Final | **10** | **10** | **10** | 0 guard blocks, median LLM 0.25 s |
| Repeat | 10 | 9 | 10 | E09 false alarm from "na sparks hain na smoke"; fixed, and E09 passed 2 of 2 rechecks |
| Skipping answered steps | 10 | 10 | 10 | 0 guard blocks, median caller turns 4.0 (unchanged); F04 6 → 5 turns in 7 of 7 runs |

The simulated caller runs on the same Sarvam model, so treat this as a regression check, not proof of real-world quality. Real calls are the test that counts.

**Voice, measured end to end** (a WebRTC client played a recorded Hindi caller into the real pipeline):
- Bulbul's greeting played, saaras:v4 codemix transcribed "AC ठंडा नहीं कर रहा।", the playbook started and the agent answered.
- **Voice-to-voice 1.08 s, then 0.56 s on a repeat** (TTS first byte 205–252 ms, LLM about 0.28 s).

**Still open from the live test:**
- **Turn-taking:** the caller's pause at a comma ended their turn early, so half a sentence got an answer. Try Pipecat's smart-turn analyser or a longer VAD stop time, and tune on real calls.
- **Capacity:** the current Sarvam plan rate-limits around a few requests per second. Each live call uses about one LLM request per caller turn, so check the plan before more than a handful of simultaneous calls.
- **Language:** the agent uses Hindi for Roman-script Hinglish now, but speech still uses the customer's profile language for the whole call.

## Skipping steps the caller already answered (2026-10-19)

Before this change, a caller who said "abhi power cut hai, inverter pe chal raha hai" up front was still asked about the remote, the wall switch and the MCB, and then asked about the power cut again. Steps are still walked in playbook order, but the agent no longer re-asks a step the caller has already answered.

**How it works:**
- **Evidence:** the model may answer a step that hasn't been read out only by passing `caller_said`, the caller's own words. The server accepts them only if they appear in one of the caller's messages on this call (same words in order; spelling variants like "hai/hain" and one extra or missing word allowed), the caller didn't negate them ("wall switch on nahi hai" doesn't count, nor does "switch off" for "switch on"), and they mention the step itself (a sentence about the power cut is no evidence for the wall switch).
- **Second check:** every skip is classified again by a short separate call that doesn't see the model's pick. The skip stands only if both agree. The eval caught one wrong skip before this was added: "saare MCB bhi upar hain" was taken as "one switch is down", and the caller was booked a technician they didn't need.
- **Never for safety:** safety questions, and any hazard outcome, can't be skipped. They are always asked, and "no hazard" still needs the caller's own denial.
- **Look-ahead:** after an answer lands on a new step, the model gets extra rounds with only `answer_step` (plus an `ask_caller` option) to check whether the caller already covered it. Only the step finally reached is spoken. Look-ahead rounds and second checks share a cap of 2 extra LLM calls per turn (about +0.25 s each). A word-overlap pre-filter skips the round when the caller never used any of the step's words.
- **Logged:** skipped steps show in the playbook path, on `/ops` and in the technician's "Tried on call" as `<answer> (answered earlier: "<caller's words>")`, plus a `step_skipped` event.

**Results** (simulated caller, same Sarvam model):
- **30 scenarios:** fixable 10/10, escalate 10/10, safety 10/10, 0 guard blocks, median caller turns 4.0, the same as before. Extra calls ran on 8 of 96 turns: median LLM time 0.57 s on those turns, 0.25 s on all others.
- **F04 (power cut):** 6 → 5 caller turns, in 7 of 7 runs.
- **Few skips in the main set:** the eval's simulated caller is told not to volunteer causes, so the 30 scenarios mostly check for regressions.
- **Four variants where the caller volunteers answers** (run separately, not in `tests/scenarios.yaml`), two runs each:
  - Power cut said first: 5 turns.
  - Everything said in the first sentence: 4–5 turns.
  - New inverter but no power cut: correctly escalated, never mistaken for a power cut.
  - English "already on Cool at 24, doors and windows closed": no skip, 7 turns. The model quoted the caller's latest message, "No, nothing like that", which was rejected. For the room step, the second check judged that "doors and windows closed" doesn't say nothing is blocking the AC.
- **Accuracy:** in the final two rounds, all 23 accepted skips were correct. Bad quotes were rejected and those steps were asked as normal.

**Limits:**
- The 2-call cap means at most one look-ahead skip per turn. A caller who lists three checks at once still hears one or two of them.
- The model still sometimes quotes the wrong message, which costs a turn but nothing worse.
- Real callers volunteer much more than the simulated one, so measure skips and wrong skips on real calls (the `step_skipped` events).

## v1.1: Bolna phone calls, Claude brain, RentBasket data (2026-10-24)

What you decided: Bolna runs the phone call with our brain as its custom LLM; Claude runs the brain;
add semi-automatic washer, fridge leak/door/noise, inverter, TV, purifier and gas stove playbooks and
drop the geyser; keep the customer WhatsApp template as Marketing for now.

What changed:
- **Brain model.** `fixfirst/claude_llm.py` runs the brain on Claude (`ANTHROPIC_MODEL`, default
  `claude-haiku-5-5`) through the Anthropic SDK. It keeps the same contract as on Sarvam: every turn is a tool
  call (forced tool choice, which also skips thinking for speed), effort `low`, no sampling parameters.
  `LLM_PROVIDER=sarvam` switches back.
- **Phone through Bolna.** `/bolna/v1/chat/completions` is an OpenAI-compatible streaming endpoint.
  Bolna sends no call id to a custom LLM, so the agent's prompt carries Bolna's variables
  (`call_ref={execution_id} from={from_number} to={to_number}`) and the bridge reads them back. Only the
  caller's new words go to the brain; a repeat request with nothing new returns the last reply; a reply
  that Bolna's history shows was cut short, or a dropped stream, marks the step unheard. Requests
  without the `FIXFIRST_CALL` marker (Bolna's own side checks) get an empty reply. `/bolna/webhook`
  closes the call and adds the recording link to the ticket. `python -m fixfirst bolna-setup` registers
  the endpoint and creates the agent; `bolna-call` places a test call.
- **RentBasket customers.** `fixfirst/rentbasket.py` looks callers up by mobile number in
  `acapi.rentbasket.com/api/get-customer-details` (Bolna calls, Exotel calls, and a "Look up" box on the
  call page) and copies them into the local tables. Product names map to appliances ("Window AC - 2009"
  → ac, "SMART LED 32\"" → tv, furniture → no playbook, so a technician is booked).
- **Playbooks** from the catalogue and the complaint log: `washer_semi` (new), `inverter`, `tv`,
  `water_purifier`, `stove` (new), fridge (leak inside/floor, noise, door, over-cooling, broken parts),
  washer (broken inlet pipe). Geyser moved to `knowledge/archive/`. All are `draft-unreviewed`.
  The water purifier and the stove's burner-cap step cite third-party pages (no official Indian
  manufacturer page was found); those need a technician's review first.
- **Safety screen.** "flame kam aa rahi hai" / "peeli flame" (a weak stove flame, a real complaint) no
  longer triggers the fire script; "aag", "fire", a gas smell or flames out of control still do.
- **Eval:** 36 scenarios (12/12/12), new ones for TV, stove, fridge leak, inverter, semi-automatic spin,
  swollen battery and purifier-on-socket. The eval uses whichever model runs the brain.
- 219 automated tests pass.

Live Claude eval (2026-10-24, `claude-haiku-5-5`, simulated caller also on Haiku):

| Run | Fixable | Escalated | Safety | Guard blocks | Median brain time | p90 |
|---|---|---|---|---|---|---|
| 1 (as built) | 9/12 | 12/12 | 12/12 | 1 | 1.16 s | 1.86 s |
| 3 (after fixes) | 12/12 | 12/12 | 12/12 | 0 | 1.12 s | 2.05 s |

Fixes between runs: (1) the AC "Cool for five minutes" step now tells the caller to press Mode until
the snowflake shows and set 24; it never said to switch. (2) If the caller hasn't finished a step ("main
abhi daal rahi hoon", "kya 3 minute wait karna hai?"), the agent acknowledges and waits instead of
answering the step or repeating it; a repeat of a step the caller already heard now gets "ठीक है, आराम
से कीजिए" instead of a guard block. (3) Eval harness: the simulated caller finishes waits in one
reply, and the cap is 14 caller turns (Haiku as the caller chats while doing steps).

Latency: Claude Haiku answers in about 1.1 s per turn, against about 0.25 s for sarvam-105b. On a phone
call that adds up to a second before each reply. Options if it feels slow: try `sarvam` as the brain
again (`LLM_PROVIDER=sarvam`, needs a Sarvam key), or keep Claude and stream a short filler.

Not verified yet (no public URL):
- A real Bolna call: the agent JSON follows Bolna's docs but hasn't been accepted by their API yet;
  the `family: openai` value for the custom LLM and Plivo as the default telephony are guesses to confirm.
- Whether Bolna substitutes `{execution_id}` in the system prompt of a custom-LLM agent. If it doesn't,
  the bridge falls back to identifying the call by its opening exchange (fine for testing, not for
  two simultaneous calls with identical openings).

Found while doing it:
- **The customer API needs no authentication.** Anyone with a mobile number gets that customer's name,
  addresses, rented items and complaint history. RentBasket should put a key on it (DPDP exposure).
- The `.env` key labelled as Sarvam was a Bolna key (`bn-…`, RentBasket's Bolna account: 2 concurrent
  calls). It is now `BOLNA_API_KEY`; browser voice needs a real Sarvam key again.
- The common-problems export is mostly "Adhoc - Service request" and "Exchange" with no description,
  so it can't size phone-fixable vs technician-only yet. Tagged tickets would.
- The customer template was categorised Marketing because its body is a greeting plus one open
  variable. A ticket-specific body (ticket number, status) should pass as Utility later.

## Known limitations of v1
- Sessions live in memory, so a server restart drops live calls. Call records and tickets are in the database.
- One problem per call: a second issue mentioned after a ticket is added to that ticket as a note.
- WhatsApp replies (YES/NO to follow-ups) are not read yet. That needs the inbound webhook.
- Text-to-speech uses one language code per call (from the customer's preference). English callers on a Hindi profile still hear the Hindi voice reading English.
- Photos are stored for the technician but not analysed. A vision model (Sarvam `gemma4` on the v2 API) could later read error codes from photos.
- The demo customers and brands are made up.

## Suggested next steps
1. Add the key, run the eval, and make 10–20 voice calls; tune the prompt and lexicon from the transcripts.
2. Tag the 90 days of tickets and decide which to emphasise: fixes on the call, or the technician handoff.
3. Technician review of the AC and washer playbooks; add model-specific branches.
4. Exotel number pilot with internal staff.
5. Inbound WhatsApp webhook (follow-up replies, voice notes as a channel).
6. A technician copilot mode (the Urban Company idea) on the same playbooks and handoff data.
