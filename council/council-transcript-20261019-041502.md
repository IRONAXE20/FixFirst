# Council transcript — 20261019-041502

## Original question

FixFirst v1 design review — the user asked to properly research and build a v1 draft in one go, consulting the council when help is needed.

## Framed question

Is this the right v1 design for FixFirst — a voice-first (Hindi/English/Hinglish) appliance first-aid agent for RentBasket (Gurgaon & Noida; free maintenance included; rents fridges, fully automatic washing machines, split/window ACs, LED TVs, microwaves, RO purifiers, inverters, gas stoves; no geysers listed; FAQ names 'AC not cooling' and 'washing machine error code')? User decisions: browser call first (phone-ready via Exotel later); Sarvam end-to-end (saaras:v4 STT, sarvam-105b-conversations LLM with tool calling, bulbul:v3 TTS); builder sources public manuals now + RentBasket SOPs later; customers-only with WhatsApp handoff to the technician. Proposed design: Pipecat WebRTC pipeline + /ws/exotel; custom agent loop with a deterministic safety screen ('switch off at mains if safe' + urgent escalation); per-appliance curated knowledge pack in context + BM25 search_manuals over all docs incl. uploaded PDFs; agent asks brand from the sticker before interpreting error codes; tools identify_customer / select_appliance / search_manuals / log_step_result / create_ticket / request_photos / mark_resolved; SQLite tickets + WhatsApp (dry-run until keys); call page with live transcript, step tracker and photo upload; ops dashboard; admin doc upload. No Sarvam key yet; fleet brands unknown publicly; error codes vary by brand/model. Stakes: unsafe advice = liability; over-caution = no savings; bad latency = hang-ups; leadership judges on v1. What should change, be cut, or be added (grounding & safety; what v1 must prove; scope)?

## Advisor responses

### The Contrarian

Your safety screen gives dangerous advice in the gas case. "Switch off at the mains" during a gas leak tells the caller to operate a switch, and a switch can spark. Gas needs its own script: touch no switches, close the cylinder regulator, open the windows, go outside, escalate. RentBasket rents gas stoves, so this will come up. Also, geysers are not in their catalogue at all. Gas stoves, RO purifiers and inverters are.

The screen will miss real emergencies. It runs on Hinglish STT output. Phrases like "kuch jal raha hai", "halka current lagta hai" or a mis-transcribed "chingari" will get past a keyword list. You need a semantic classifier as well, and the screen must err toward escalating.

"Must not give steps outside the pack" is a request to the model, not something the system enforces. Enforce it: the LLM outputs only step IDs, and TTS reads pre-written scripts that RentBasket ops have approved in Hindi and English. That fixes hallucinated steps, the copyright exposure, and some of the latency. Cut search_manuals from v1.

Drop error-code interpretation. Capture the code plus a photo for the technician instead.

The core assumption has not been tested. Maintenance is free, so a customer has no reason to spend six minutes fixing it themselves. Pull 3–6 months of tickets and label them. If under ~15% are trivial, customer self-fix is the wrong product; the real value is pre-diagnosis that raises the technician's first-visit fix rate — the Urban Company copilot idea.

Latency is the risk most likely to kill this, and building on mocks hides it. Cut the ops dashboard and admin PDF upload.

### The First Principles Thinker

What problem are we actually solving? Under free maintenance, RentBasket pays for every technician visit. FixFirst only matters if it (1) prevents visits that never needed a technician, or (2) makes needed visits fixed in one trip. Nobody has measured either. Measuring them is the real v1.

Step zero isn't code: it's RentBasket's ticket history. If trivial fixes are ~5% of visits, phone-fixing saves almost nothing and the value is in the handoff. If 25%+, build for phone fixes. This one spreadsheet decides scope.

"Brands unknown" is a fake constraint. Procurement knows the exact models, probably 3–6 per category. You don't need RAG over copyrighted public manuals, BM25 or PDF upload. You need one hand-written decision tree per model, ~15 nodes, reviewed by RentBasket's senior technician.

Grounding should be structural. Make the LLM a conversational layer over a state machine whose only actions are next_step(node_id) and escalate(reason). Unsafe advice becomes impossible, and leadership can audit every path.

Cut search_manuals, admin doc upload, multi-company config, geyser, fridge. Keep AC and washing machine.

What v1 must prove: run 50 real past tickets through the bot; report % resolved safely, zero safety violations, and technician-rated handoff usefulness. Voice latency is a polish problem; this is the adoption problem.

### The Expansionist

The design treats FixFirst as a phone bot that cuts support costs. The bigger prize is that every call produces structured fleet data RentBasket doesn't have today.

1. Make the nameplate photo step one: extract brand, model and serial into the asset record — over months, a fleet registry with failure history per unit.
2. Pitch v1 on visits avoided where nothing was actually broken, in rupees.
3. Use the summer peak: AC tickets spike April–June when technicians are fully booked. Add outbound calls: a pre-summer 'clean the filter, check the mode' call and an onboarding call after delivery.
4. Turn the technician handoff into the Urban Company play: structured summary, steps ruled out, photos and likely part category raise first-visit fix rate. Track it.
5. Add WhatsApp voice notes as a channel — same brain, async, no telephony cost.
6. Config-driven opens a second market: Furlenco, Rentomojo, PG/co-living, extended-warranty firms. Design the knowledge-pack format as a product now.

Also: after a phone fix, ask for a Google review.

### The Outsider

Your customer has no reason to troubleshoot — repairs are free. The agent has to sell speed in its first line: "Most 'not cooling' problems get fixed in 3 minutes, or I can book a technician right now." Offering a technician should take one sentence at any point in the call, or you put the 4.9 rating at risk.

You work at RentBasket, so why are the brands "unknown"? Get the purchase or asset list and drop "read the sticker" — on a split AC that means climbing a chair. Also, RentBasket doesn't list geysers; it rents gas stoves, RO purifiers and inverters.

The safety rule is wrong for gas: don't touch switches, close the regulator, open windows, leave. Hinglish callers may not know what "mains" or "MCB" means — describe what it looks like and where to find it.

What v1 must prove: "X% of AC-not-cooling and washer-error calls closed without a visit, and a WhatsApp check 24 hours later confirmed they stayed fixed." Without that follow-up, "resolved" only means the caller hung up.

Scope: start with the two FAQ issues. Cut admin upload and PDF search. The live transcript and step tracker assume the caller can look at a screen; someone at a washing machine with wet hands won't.

### The Executor

You can build this design, but the order is wrong: both killer unknowns are left for last.

Monday morning: (1) Get the Sarvam key today; record 20 real complaints; time a full voice-to-voice turn. If it's over ~2 seconds, change models now. (2) Walk over to ops: pull the inventory brand/model list and the last 90 days of tickets; tag customer-fixable vs technician-only — that percentage is the business case. (3) Write 30 test scenarios (10 fixable, 10 escalate, 10 safety, including STT-mangled Hinglish like 'jalne ki smell' and 'current lag raha'). Run them text-only before any voice work.

Cut: fridge and geyser; search_manuals and admin PDF upload; reading error codes (capture the code and a sticker photo into the ticket); the /ws/exotel endpoint (phone later is a transport swap); the dashboard beyond a ticket list.

Add: real WhatsApp messages, not dry-run — a test number is about an hour, and the message arriving on a phone sells the demo.

Scorecard for leadership: % of fixable cases resolved by phone, 10/10 safety cases caught, zero steps outside the pack, median turn latency.

## Peer reviews

Anonymization key: A = The Executor, B = The Expansionist, C = The Contrarian, D = The Outsider, E = The First Principles Thinker

**Reviewer 1.** Strongest: C — the only response that catches a flaw that could hurt someone (gas + switch), sees the keyword screen failing on Hinglish STT, makes grounding system-enforced via step IDs, and sets a falsifiable test (~15% trivial tickets). E is a close second but wrong to call latency 'polish'. Biggest blind spot: B — all expansion, misses the gas flaw; nameplate-first means climbing a chair. All five missed: renter incentives and legal exposure — renters may fear damage charges, hide misuse, or read photo requests as evidence-gathering; the rental agreement must define allowed self-fix actions; DPDP Act 2023 consent for recordings, transcripts and photos, and where Sarvam keeps data.

**Reviewer 2.** Strongest: C (D also caught gas; E's state machine is the same idea but latency-as-polish is wrong). Biggest blind spot: B — a v3 roadmap that would sink v1. All five missed: who handles escalations at 11pm — safety cases need a live callback deadline with a named on-call owner, plus gas emergency and 112 numbers read aloud; the caller is often a maid or flatmate, not the account holder; tampering liability — legal and ops should sign off each step; DPDP consent.

**Reviewer 3.** Strongest: C, E close second. Biggest blind spot: B — adds outbound calls, voice notes, a registry and a second market to a v1 with no key, no latency number and no safety testing. All five missed: 'urgent escalation' has no real recipient — gas/fire/shock need a staffed on-call line and the agent should tell callers to dial 112 or the LPG emergency line 1906; DPDP consent for voices, transcripts and in-home photos.

**Reviewer 4.** Strongest: C. Biggest blind spot: B (E's latency-as-polish is a smaller miss). All five missed: where an urgent escalation actually goes — scripted referral to 1906/112 plus a staffed human line with a response-time target incl. after-hours; DPDP consent; whether the rental agreement lets customers open or handle units RentBasket owns.

**Reviewer 5.** Strongest: C; E close second; D caught gas too. Biggest blind spot: B — asking for a review after an unconfirmed fix invites backlash. All five missed: DPDP consent line + retention policy and legal sign-off on safety scripts; failure modes — appliance noise muddies audio and the Sarvam API can time out, so on low STT confidence or API failure go straight to ticket + WhatsApp; inverter batteries (acid leak, swelling, hydrogen) are missing from the safety screen.

## Chairman's synthesis

### Where the council agrees

- The gas script is dangerous: 'switch off at the mains' means flipping a switch that can spark. Gas needs its own script — touch no switches, close the regulator, open windows, go outside, call 1906. RentBasket rents gas stoves and inverters, not geysers.
- Telling the model to stay inside the knowledge pack doesn't make it stay there. Steps should be a finite, pre-written, auditable set; search_manuals, BM25 and PDF upload should not be a grounding path.
- Don't interpret error codes from guesswork — record the code and a photo for the technician.
- Narrow the scope to AC not cooling and washing machine error / won't start — the two issues RentBasket's FAQ names.
- A keyword safety screen will miss Hinglish once speech-to-text garbles it ('jalne ki smell', 'current lag raha').
- The business case is unproven: maintenance is free, so the share of no-fault visits decides what the product should be.

### Where the council clashes

- Latency — First Principles calls it polish; Contrarian and Executor call it the likeliest killer. Chairman sides with them: the build must log per-turn latency until it can be measured.
- Phone fix vs technician handoff — not in conflict: every call ends in a structured handoff, so v1 builds both and ticket data decides the emphasis.
- Ambition — the Expansionist's registry, outbound calls, voice notes and second market are good later, but only the structured handoff summary makes v1.
- Reading the sticker — Expansionist wants a nameplate photo first; Outsider notes a split AC means climbing a chair. Ask for a photo only when escalating.

### Blind spots the council caught

- 'Urgent escalation' goes nowhere without a named on-call person, an after-hours response target, and the agent reading out 112 and 1906.
- Privacy law (DPDP Act 2023): consent line, retention policy, and where Sarvam stores data.
- The rental agreement must say what renters may touch; renters may read photo requests as evidence-gathering.
- The caller is often a maid or flatmate, not the account holder.
- Failure handling: appliance noise, API timeouts or low-confidence transcripts should go straight to a ticket, not into retry loops.
- Inverter batteries (acid leak, swelling, hydrogen) were missing from the safety screen.
- Asking for a review before a fix is confirmed invites backlash.

### The recommendation

Keep the stack (Pipecat WebRTC, Sarvam, SQLite, WhatsApp) but replace how answers are grounded: answers come from a fixed structure, not retrieval. Build now: (1) decision trees stored as data for ac_not_cooling and washer_error plus shared 'nothing's broken' checks, each step written in Hindi and English, paraphrased from public manuals, tagged DRAFT-unreviewed; the server rejects any step not reachable from the current one and scripts are spoken word for word. (2) Separate safety scripts per hazard (electrical, gas, water near power, battery, smoke/fire); two screens every turn (fuzzy Hinglish lexicon incl. mis-transcriptions + a model hazard flag); either one escalates. (3) Opening line: consent, speed pitch, 'say technician anytime'. (4) Fallback to ticket + WhatsApp on low-confidence transcripts, API timeouts or frustration. (5) Per-turn latency logging. (6) Text-only test harness with 30 scenarios. (7) Structured technician handoff. (8) Voice-first call page, ticket-list ops view, transport interface for Exotel later, WhatsApp dry-run switched live by one env var. Cut: search_manuals, PDF upload, other appliances, error-code lookup, outbound calls, the review ask, multi-company config. Asks for RentBasket: Sarvam key + 20 recorded complaints timed; 90 days of tickets tagged; fleet model list; senior technician + legal sign-off on scripts and a rental-agreement clause; named on-call owner and after-hours target; WhatsApp Business keys; DPDP consent wording and retention policy; cost of one technician visit.

### The one thing to do first

Before writing any pipeline code, write the 30-scenario test file and make the safety screen pass all 10 safety cases — including a gas case whose response contains no instruction to touch a switch.

POSITIONS: Contrarian=support, First Principles=support, Expansionist=mixed, Outsider=support, Executor=support

## How the builder applied it

How the builder applied this: adopted structural grounding (playbooks as data, verbatim scripts, server-validated transitions), per-hazard safety scripts with two screens, consent + speed-pitch opening, fallback-to-ticket, latency logging, the scenario harness and the structured handoff. Deliberately kept, against the council, because the user's brief names them: a document library with upload + search (used for step citations; an agent doc-lookup tool ships switched off), starter playbooks for fridge and geyser alongside full AC and washing machine ones, an error-code table limited to official LG India / Samsung India sources (unknown codes are captured for the technician), and a small /ws/exotel route because the user chose 'phone-ready'.
