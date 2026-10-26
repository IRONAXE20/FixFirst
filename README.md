# FixFirst

**Voice-first appliance first aid for rental and service companies, starting with RentBasket.**

A customer calls when an appliance they rent stops working (AC, fridge, washing machine or microwave), describes it in Hindi, English or Hinglish, and an AI agent walks them through safe checks one step at a time. If it can't be fixed by phone, FixFirst books a ticket, sends the customer a WhatsApp with a photo-upload link, and sends the service team a technician handoff (steps already ruled out, likely cause, parts to carry). When the problem is closed, the agent asks "anything else?" and, if not, says goodbye and ends the call.

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

After a problem is fixed, booked or set for a follow-up, the agent asks "क्या मैं आपकी और कोई मदद कर सकती हूँ?". A "no / thanks / bye" gets "…आपका दिन शुभ हो!", and Bolna's hang-up check (sent to our endpoint after each reply) is answered "yes" only then. The end-of-call webhook closes the call record and adds the recording link to the ticket.

**Why it can only say what the playbooks support.** Every physical instruction is a step in a playbook: a decision graph where each step has `say.en` / `say.hi`, the allowed `outcomes` (each leading to the next step, a safety hazard or an ending), and the `sources` it was paraphrased from (`knowledge/sources.yaml`). The model only classifies the caller's answer into one of the current step's outcomes; the server rejects an outcome that isn't listed, an answer to a step the caller hasn't heard, or a jump that isn't declared.

**Safety.** Two independent screens run every turn: the lexicon (negation-aware, tuned for speech-to-text Hinglish, quiet on everyday phrases like "current aa raha hai", "AC ki gas khatam" or a low stove flame), and the model's `flag_safety_concern` tool. A water alarm needs the caller to mention a plug, switch, board or wiring; "AC se paani aa raha hai" gets a "near a plug, or only dripping?" question instead. Any hazard ends troubleshooting, reads that hazard's own script (gas never mentions a switch: close the regulator, open windows, go outside, call 1906), books an URGENT ticket and alerts the team; after that the agent only repeats the hold line.

## Playbooks (stored in Supabase)

The playbooks come from **RentBasket's Appliance Troubleshooting SOP** (`knowledge/sop/`, v1.0, 25 Oct 2026):

| Playbook | SOP part | Covers |
|---|---|---|
| `ac_not_cooling` | Part A, AC-01 to AC-12 | Lloyd / Voltas split and window AC |
| `fridge_not_cooling` | Part B, FR-01 to FR-13 | Whirlpool / Lloyd, direct-cool and frost-free |
| `washer_problem` | Part C, WM-01 to WM-12 | Whirlpool top-load, front-load and semi-automatic |
| `microwave_problem` | Part D, MW-01 to MW-10 | Haier / Panasonic / Midea |

- Each starts with the SOP's safety gate; the triage question routes to one SOP playbook, and every step is one instruction with yes/no-style answers. "Technician" works at every step.
- The SOP's boundaries are built in: no ladders, stools or work at height, no switchboards or MCB resets, no drain, pump or inlet filters, no tightening hoses, no moving or tilting appliances, no unplugged defrost, no feet adjusting, one normal restart at most. A breaker that keeps tripping, a damaged microwave door, a washer moving violently and a chemical smell are stop-use hazards (P0).
- Every technician ticket names the SOP playbook, its issue code and priority (for example `AC-01 AC_NO_COOLING (P1)`) with the SOP's technician notes, including that refrigerant work goes to a qualified technician (not Digamber).
- Error codes are recorded exactly and never interpreted, as the SOP requires; the old LG/Samsung code table is in `knowledge/archive/`.
- TV, inverter, water purifier, gas stove and OTG aren't in the SOP: those calls go straight to a technician. Their old playbooks are in `knowledge/archive/`.
- The visit promise names no day or time (SOP section 7).

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
- All playbooks are `draft-unreviewed` until RentBasket signs off the spoken wording: the SOP gives checks, not scripts, so the English and Hindi sentences are FixFirst's.

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

## WhatsApp (not live yet)

The phone number ID and business account ID are set. To go live you also need:
1. A permanent **access token** (Meta Business Settings → System user, permission `whatsapp_business_messaging`) in `WHATSAPP_TOKEN`.
2. Approved templates `fixfirst_customer_update` and `fixfirst_team_ticket` (one body variable `{{1}}` each). The customer template is currently categorised Marketing.
3. The team's numbers in `knowledge/tenant.yaml` (`team_whatsapp`).
4. `WHATSAPP_MODE=live`.

Until then every message lands in the outbox on `/ops`.

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
| `knowledge/` | RentBasket's SOP, playbook seeds, sources, safety scripts, tenant settings, document library, archive |
| `render.yaml`, `requirements-render.txt` | Render deployment |

## Status

- **Live calls through Bolna work end to end:** call matching from webhooks, Claude replies in 1–2.7 s per turn (first answer ~1.3 s), safety alerts, technician booking, the end-of-call webhook. Fixes from five test calls are in.
- **Eval on Claude:** fixable 12/12, escalations 12/12, safety 12/12, 0 guard blocks (the simulated caller occasionally contradicts its own script, so run it more than once).
- **245 automated tests** pass without any key.
- **Not yet verified:** the goodbye and hang-up on a live call, recognising a real customer on a live call, live WhatsApp, inbound calls, photos and playbook editing on the live Supabase.
- **Before a pilot:** Render Starter, WhatsApp token and templates, a restricted Supabase login instead of `postgres`, technician sign-off of every playbook, and a key on RentBasket's customer API.

See [docs/v1-review.md](docs/v1-review.md) for the decisions, test history and what to ask RentBasket.
