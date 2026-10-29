# FixFirst

**Voice-first appliance first aid for rental and service companies, starting with RentBasket.**

A customer calls when something they rent has a problem (AC, fridge, washing machine, microwave, water purifier, sofa, bed or dining table) and describes it in Hindi, English or Hinglish. Following RentBasket's knowledge base, the agent either raises a ticket straight away or, for the few issues the customer can fix, walks them through it one step at a time. For a ticket, FixFirst sends the customer a WhatsApp with a photo-upload link, and sends the service team a technician handoff (steps already ruled out, likely cause, parts to carry). When the problem is closed, the agent asks "anything else?" and, if not, says goodbye and ends the call.

**Live setup (October 2026):**

| Part | Runs on |
|---|---|
| Phone calls | **Bolna** (telephony, Sarvam saaras:v4 speech-to-text, bulbul:v3 voice "priya"), calling FixFirst as its custom LLM |
| The brain | **Claude** `claude-haiku-5-5` (or `sarvam-105b-conversations` with `LLM_PROVIDER=sarvam`) |
| Server | **Render** web service `fixfirst`, auto-deploys from GitHub `main` |
| Data | **Supabase** Postgres, in its own `fixfirst` schema: customers, calls, tickets, WhatsApp outbox, follow-ups, **photos** and **playbooks** |
| Customers | Looked up by phone number in **RentBasket's customer API** |
| WhatsApp | Meta Cloud API, **dry-run** until an access token is set |

A browser voice call (Pipecat, WebRTC) also works locally with a Sarvam key; it can't run on Render, which doesn't accept the UDP traffic WebRTC needs.

## Run it locally

```bash
python -m venv .venv
```
```bash
.venv\Scripts\python -m pip install -r requirements.txt
```
```bash
copy .env.example .env
```
```bash
.venv\Scripts\python -m fixfirst serve
```

Open http://localhost:7860.

- **With `ANTHROPIC_API_KEY`:** Type mode runs the real agent. Look up a real RentBasket customer by mobile number, or pick a demo customer.
- **With a Sarvam key (`sk_...`) too:** Voice works in the browser (headphones avoid echo).
- **With no key:** Type mode runs an offline keyword demo brain (simple phrases only), so you can still click through playbooks, tickets and the ops view.

Pages:
- `/` call page: live step card, transcript, photo upload, outcome.
- `/ops` KPIs, ticket handoffs (close a ticket here), call transcripts with per-turn latency, and the WhatsApp outbox.
- `/knowledge` every playbook step in Hindi and English with its source, an **editor** for playbooks, the safety scripts, and the document library.

Tests (no key needed) and the live scorecard (needs a model key):
```bash
.venv\Scripts\python -m pytest -q
```
```bash
.venv\Scripts\python -m fixfirst eval
```

## How a call works

```
caller ── phone ──▶ Bolna (telephony + Sarvam speech-to-text, turn-taking)
                       │  POST /bolna/v1/chat/completions   (FixFirst as Bolna's "custom LLM")
                       ▼
        ┌── FixFirst (fixfirst/bolna.py → fixfirst/brain.py) ─────────────────┐
        │ 0. Which call? Bolna's status webhooks bring the call id and phone   │
        │    numbers ~40 s before the first words; the RentBasket lookup runs  │
        │    then, so the agent knows the caller's rentals on turn one.        │
        │ 1. Hazard screen: deterministic Hindi/Hinglish/English lexicon       │
        │    → per-hazard safety script, URGENT ticket, team alert             │
        │ 2. Claude picks a tool: start_troubleshooting · answer_step ·        │
        │    explain_step · record_error_code · escalate_to_technician ·       │
        │    flag_safety_concern · mark_resolved · add_note · say              │
        │ 3. The server runs the playbook: the next step is spoken WORD FOR    │
        │    WORD; the model's own words pass a guard that blocks improvised   │
        │    repairs. Only tools offered on that turn may run.                 │
        └──────────────────────────────────────────────────────────────────────┘
                       ▼  streamed reply
                     Bolna speaks it (Sarvam bulbul:v3)
```

### The call script (RentBasket Customer Service Ecosystem, 29 Oct 2026)

The voice agent follows section 3 of RentBasket's ecosystem document:

1. **Greeting:** "नमस्ते! मैं RentBasket की FixFirst service assistant बोल रही हूँ। आपकी क्या मदद कर सकती हूँ?" Hinglish to start; the caller's replies decide Hindi or English.
2. **Identify** (phone calls): a registered caller is asked "आप {name} जी बोल रहे हैं? आपका address {area} है ना?". If the number isn't on any deal, the agent asks for a roommate's or family member's number, sends an **OTP** to that number on WhatsApp (template T10a) and the caller reads it back; the number is then linked to that account for future calls. A wrong OTP twice, or no other number, goes to a person.
3. **Pick the product:** one of that type is confirmed ("आपके पास Voltas Split AC rent पर है। उसी के बारे में बात कर रहे हैं?"); several are named ("एक Voltas और एक Lloyd, किस वाले में?").
4. **Capture the issue** ("problem थोड़ा बता दीजिए") and match it to a KB issue, confirming when unsure.
5. **Safety gate on every turn:** switch off at the wall, URGENT ticket, "हमारी team जल्दी से जल्दी call करेगी" (callback within 1 hour plus a same-day visit, for the team).
6. **KB action:** TICKET: "मैंने आपका ticket raise कर दिया है…", the ticket number, and the WhatsApp photo-and-notes link. SELF-FIX: "क्या आप अभी {product} के पास हैं?", then one step at a time and "क्या अब ठीक चल रहा है?"; not near (and always for FR-04, which takes 12 hours) the steps go on WhatsApp (T2).
7. **Possible misuse:** "मैं ये सारी details हमारी team को भेज रही हूँ…", `audit_required` on the ticket for the Auditing team; the agent never decides or mentions charges.
8. **Human handoff** when the caller asks for a person, is angry, the issue isn't in the KB, or the agent fails to understand twice: 9am–8pm IST a warm transfer to the care number (+91 9958187021, Bolna's transfer tool); outside those hours a ticket and "हमारी team कल सुबह 9 बजे के बाद आपको call करेगी".
9. **Closing:** "और कुछ मदद चाहिए?", then "Thank you {name} जी। RentBasket choose करने के लिए शुक्रिया। Have a good day!", and the call ends (Bolna's end_call tool when offered, else its hang-up check).

Steps 2 and 3 run on phone calls only and can be turned off with `IDENTITY_CHECK=false`. To call in as a demo customer from your own phone, set `TEST_CALLERS=98XXXXXXXX=C1` (nothing goes to real customers, no OTP).

### Tickets

One FF-xxxx series. Lifecycle: NEW → SELF_FIX_IN_PROGRESS → RESOLVED_REMOTE, or TICKET_RAISED / URGENT → SCHEDULED → IN_PROGRESS → FIXED / REPLACED → (AUDIT_REVIEW) → CLOSED; the same problem on the same appliance within 7 days of a fix **reopens** the old ticket. Every ticket records its KB issue, `origin_channel` (voice | whatsapp), `channels_used` (voice_only | both once a WhatsApp went out) and `audit_required`. The team moves tickets along on `/ops`; FIXED or REPLACED schedules the 24-hour check (T9). SLA shown to the team: agree a visit within 24 h (appliances) or 48 h (furniture).

The customer has **one persistent link** (`/u/<token>`, no OTP) for photos and the note "Anything else you missed telling us on the call?"; with several open tickets they pick which one. The link is replaced when the mobile number changes and stops working when the rental ends.

**Why it can only say what the playbooks support.** Every physical instruction is a step in a playbook: a decision graph where each step has `say.en` / `say.hi`, the allowed `outcomes` (each leading to the next step, a safety hazard or an ending), and the `sources` it was paraphrased from (`knowledge/sources.yaml`). The model only classifies the caller's answer into one of the current step's outcomes; the server rejects an outcome that isn't listed, an answer to a step the caller hasn't heard, or a jump that isn't declared.

**Safety.** Two independent screens run every turn: the lexicon (negation-aware, tuned for speech-to-text Hinglish, quiet on everyday phrases like "current aa raha hai", "AC ki gas khatam" or a low stove flame), and the model's `flag_safety_concern` tool. A water alarm needs the caller to mention a plug, switch, board or wiring; "AC se paani aa raha hai" gets a "near a plug, or only dripping?" question instead. Any hazard ends troubleshooting, reads that hazard's own script (gas never mentions a switch: close the regulator, open windows, go outside, call 1906), books an URGENT ticket and alerts the team; after that the agent only repeats the hold line.

## Playbooks (stored in Supabase)

The playbooks come from **RentBasket's Service Knowledge Base v1** (`knowledge/sop/RentBasket_Issue_KB.md`), which takes precedence over their longer Appliance Troubleshooting SOP (kept in the same folder as background):

| Playbook | KB issues | Self-fix (guided step by step) |
|---|---|---|
| `ac_not_cooling` | AC-01 to AC-04 (Lloyd / Voltas) | AC-04 weak airflow: clean the filters. AC-03 asks two safety questions first |
| `fridge_not_cooling` | FR-01 to FR-05 (Whirlpool / Lloyd) | FR-04 front leak: switch off, empty, door open with a fan for 12 hours |
| `washer_problem` | WM-01 to WM-05 (Whirlpool) | WM-01 water not coming in: clean the inlet filter |
| `microwave_problem` | MW-01 to MW-04 (Haier / Panasonic / Midea) | MW-04 bad smell: unplug and wipe the inside |
| `water_purifier_problem` | WP-01 to WP-03 | none |
| `furniture_problem` | SF-01/02 sofa, BD-01/02 bed, DT-01 dining table | none |

- Everything the KB marks **ACTION = TICKET** gets a ticket straight away, with no fix suggested, in the KB's own words ("Maine aapka ticket raise kar diya hai. Hamare technician jaldi aapse contact karenge."). If the caller already named the issue, that happens on their first turn.
- Self-fixes are one step at a time and end with "did it work?"; if not, a ticket. An issue the KB doesn't list gets a ticket too.
- Triage matches the caller's words against the KB's "Customer says" phrases.
- Safety: smoke, sparks, burning smell, shock or water near a plug are caught on every turn; MW-01 (sparking or loud noise), a burnt socket or damaged wire (AC-03), a burning smell from the microwave, and water near the plug (FR-04, WM-05) are urgent stop-use tickets.
- Every ticket names its KB issue (for example `AC-01 Not cooling`), plus a technician note (refrigerant work: a qualified technician, not Digamber).
- Error codes are recorded exactly and never interpreted.
- TV, inverter, gas stove and OTG aren't in the KB: those calls go straight to a ticket. The SOP-based playbooks are in `knowledge/archive/sop/` (the engine tests still run on them), older ones in `knowledge/archive/`.
- The ticket promise names no day or time.

- Stored in the `fixfirst.playbooks` table, one row per appliance, with every saved version in `fixfirst.playbook_history`.
- **Edit on `/knowledge` → ✏️ Edit.** The editor shows the playbook as YAML. **Check** validates it; **Save** needs the admin key (`ADMIN_KEY`) and stores a new version that calls use straight away. Nothing is saved if a step is missing a language, an answer leads nowhere, a step can't be reached, or a source doesn't exist. If two people edit at once, the second save is refused until they reload.
- `knowledge/playbooks/*.yaml` seed an empty table and add any playbook the table doesn't have yet. Once a playbook is in the table, **the table wins** over the file.
- Copy between the two:
  ```bash
  .venv\Scripts\python -m fixfirst playbooks export
  ```
  writes the database copy to `knowledge/playbooks/` (to keep git in step), and
  ```bash
  .venv\Scripts\python -m fixfirst playbooks import
  ```
  stores every YAML file as a new version (overriding edits made on `/knowledge`).
- Running `playbooks import` also retires playbooks that no longer have a YAML file, so the archived ones stop being served.
- All playbooks are `draft-unreviewed` until RentBasket signs off the spoken wording: the KB gives the actions, not full scripts, so most English and Hindi sentences are FixFirst's.

## Photos

Customer photos (from the WhatsApp upload link or the call page) are stored in Supabase (`fixfirst.photos`), not on the server's disk, which Render wipes on every deploy. They're served at `/media/p/<id>`. A photo is **deleted 7 days after its ticket is closed** (`PHOTO_RETENTION_DAYS`; close tickets on `/ops`), and a photo never attached to a ticket after 30 days. The clean-up runs hourly.

## Hosting on Render

- Service `fixfirst` (Singapore), build `pip install -r requirements-render.txt` (no Pipecat), start `python -m fixfirst serve --host 0.0.0.0`. `render.yaml` describes the same service as a Blueprint, for moving it to RentBasket's Render workspace.
- Every push to `main` deploys in about 3 minutes.
- Secrets in Render's Environment tab: `ANTHROPIC_API_KEY`, `DATABASE_URL`, `FIXFIRST_LLM_KEY` (the same value as in your local `.env`, because `bolna-setup` registers it with Bolna), `ADMIN_KEY`, and later `WHATSAPP_TOKEN`.
- On boot the server warms up (one database round trip and one tiny model request), so the first answer of a call takes about 1.8 s instead of 14 s.
- **The Free plan sleeps after 15 idle minutes** and takes ~45 s to wake. Bolna's first webhook usually wakes it before the caller speaks, but use Starter (or RentBasket's Pro workspace) for a pilot.

## Phone calls through Bolna

1. Put `ANTHROPIC_API_KEY` and `BOLNA_API_KEY` in `.env`.
2. Point Bolna at the server (run again after changing the URL or call settings):
   ```bash
   .venv\Scripts\python -m fixfirst bolna-setup --public-url https://fixfirst-zgo0.onrender.com
   ```
   This registers `<url>/bolna/v1` as a Bolna custom LLM (protected by `FIXFIRST_LLM_KEY`) and creates or updates the agent: Sarvam saaras:v4 + bulbul:v3 "priya", Hindi welcome line, 800 ms end-of-turn pause, hang-up check on, 90 s silence limit, and the end-of-call webhook (`/bolna/webhook/<token derived from the key>`). It saves `BOLNA_AGENT_ID` to `.env`.
3. Test from the Bolna dashboard, or have the agent call you:
   ```bash
   .venv\Scripts\python -m fixfirst bolna-call 98XXXXXXXX
   ```
   To test customer recognition from your own phone, add `--as <a RentBasket customer's number>`. The agent then looks you up as that customer, and the call is marked as a test, so no WhatsApp message or follow-up goes to the real customer.

Calls go through Bolna's own hosted numbers; no separate Plivo account is needed. For customers to dial in, buy an inbound number inside Bolna (Plivo or Vobiz) and attach the agent. The Bolna account allows 2 calls at once.

## WhatsApp

Live from RentBasket's number (+91 99580 96702). Two template sets:
- **v1** (approved Utility templates, in use): `fixfirst_service_request` for the customer (name, ticket, product, promise or safety line, and a URL button with the customer's link token) and `fixfirst_team_ticket` for the team. `fixfirst_customer_update` is Marketing and isn't used. Self-fix steps and check-ins have no approved Utility template yet, so they're logged as `no_template` on `/ops` and not sent, and a caller who can't do a self-fix now gets a ticket instead of WhatsApp steps.
- **ecosystem** (`WHATSAPP_TEMPLATES=ecosystem` once Meta approves them): T1 `technician_booked` with the "Upload photo or details" button, T2 `selffix_steps_ac04/fr04/wm01/mw04`, T4 `urgent_safety`, T9 `followup_check`, T10a `otp_verify`. The exact text to submit is in [docs/whatsapp-templates.md](docs/whatsapp-templates.md). Until then an unregistered caller can't get an OTP and goes to a person.

The team's numbers go in `knowledge/tenant.yaml` (`team_whatsapp`); until then team messages land in the outbox on `/ops`. The 48-hour follow-up call (when the 24-hour WhatsApp gets no reply) needs an outbound telephony number on Bolna and isn't built yet.

## Project layout

| Path | What it is |
|---|---|
| `fixfirst/brain.py` | The agent: hazard screen → model tool choice → scripted playbook steps, guard, goodbye, fallbacks, latency |
| `fixfirst/claude_llm.py` | Claude as the brain's model (Anthropic SDK) |
| `fixfirst/bolna.py` | Bolna custom-LLM bridge: call matching from webhooks, streaming replies, hang-up check, end of call |
| `fixfirst/bolna_admin.py` | `bolna-setup` / `bolna-call` commands |
| `fixfirst/rentbasket.py` | Customer lookup from RentBasket's API, copied into the local tables |
| `fixfirst/playbooks.py` | Parses, validates and runs playbooks; the error-code lookup (empty: codes aren't interpreted) |
| `fixfirst/playbook_store.py` | Playbooks in the database: seed, versioned save, import/export |
| `fixfirst/photos.py` | Photos in the database and their retention |
| `fixfirst/safety.py` | Hazard screen and the unsafe-instruction guard |
| `fixfirst/server.py` | FastAPI pages and APIs, Bolna endpoints, warm-up, follow-up and clean-up loop |
| `fixfirst/tickets.py` | Tickets, technician handoff, WhatsApp messages, follow-ups, call records |
| `fixfirst/whatsapp.py` | Meta Cloud API sender with a dry-run outbox |
| `fixfirst/voice.py` | Browser/Exotel voice through Pipecat (local only) |
| `fixfirst/eval.py` | Plays `tests/scenarios.yaml` (36 scenarios) against the real agent and prints the scorecard |
| `knowledge/` | RentBasket's KB and SOP, playbook seeds, sources, safety scripts, tenant settings, document library, archive |
| `render.yaml`, `requirements-render.txt` | Render deployment |

## Status

- **Live calls through Bolna work end to end:** call matching from webhooks, Claude replies in 1–2.7 s per turn (first answer ~1.3 s), safety alerts, technician booking, the end-of-call webhook. Fixes from five test calls are in.
- **Eval on Claude:** fixable 12/12, escalations 12/12, safety 12/12, 0 guard blocks (the simulated caller occasionally contradicts its own script, so run it more than once).
- **Automated tests** (`pytest`) pass without any key.
- **Not yet verified:** the goodbye and hang-up on a live call, recognising a real customer on a live call, live WhatsApp, inbound calls, photos and playbook editing on the live Supabase.
- **Before a pilot:** Render Starter, WhatsApp token and templates, a restricted Supabase login instead of `postgres`, technician sign-off of every playbook, and a key on RentBasket's customer API.

See [docs/v1-review.md](docs/v1-review.md) for the decisions, test history and what to ask RentBasket.
