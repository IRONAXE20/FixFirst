# FixFirst

**Voice-first appliance first aid for rental and service companies, starting with RentBasket.**

A customer calls when an appliance they rent stops working (AC, washing machine, fridge, TV, inverter, water purifier or gas stove), describes it in Hindi, English or Hinglish, and an AI agent walks them through safe checks one step at a time. If it can't be fixed by phone, FixFirst books a ticket, sends the customer a WhatsApp with a photo-upload link, and sends the service team a technician handoff (steps already ruled out, likely cause, parts to carry).

Two ways to call:
- **Phone, through Bolna.** Bolna runs the call (telephony, Sarvam saaras:v4 speech-to-text, bulbul:v3 voice) and calls FixFirst as its custom LLM. Callers are recognised by phone number from RentBasket's customer API.
- **Browser** (WebRTC, Pipecat) for demos. This needs a Sarvam key; the same bot also listens on `/ws/exotel`.

The brain runs on **Claude** (`claude-haiku-5-5` by default) or on `sarvam-105b-conversations`.

## Try it in 2 minutes

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

- **With `ANTHROPIC_API_KEY` in `.env`:** **Type** mode runs the real agent. Look up a real RentBasket customer by mobile number, or pick a demo customer.
- **With a Sarvam key (`sk_...`) too:** **Voice** works in the browser. Headphones avoid echo.
- **With no key:** Type mode runs an offline keyword demo brain (simple phrases only; not the agent), so you can still click through playbooks, tickets and the ops view.
- **Phone calls:** see "Phone calls through Bolna" below.

Pages:
- `/` is the call page: live step card, optional transcript, photo upload, outcome.
- `/ops` shows KPIs (fixed-on-call rate, tickets, safety alerts, voice-to-voice latency), ticket handoffs, call transcripts with per-turn latency, and the WhatsApp outbox.
- `/knowledge` holds every playbook step in Hindi and English with its source, the safety scripts, and the document library (upload and search).

Tests (no key needed) and the live scorecard (needs the key):
```bash
.venv\Scripts\python -m pytest -q
```
```bash
.venv\Scripts\python -m fixfirst eval
```

## How it works

```
caller ──audio──▶ Pipecat (WebRTC in the browser, or Exotel websocket for phone)
                    │  Silero VAD + barge-in
                    ▼
                  Sarvam saaras:v4 (speech-to-text, hi-IN)
                    ▼
            ┌── FixFirst brain (fixfirst/brain.py) ─────────────────────────┐
            │ 1. Hazard screen: deterministic Hindi/Hinglish/English lexicon │
            │    → per-hazard safety script, URGENT ticket, team alert       │
            │ 2. sarvam-105b-conversations picks a tool:                     │
            │    start_troubleshooting · answer_step · explain_step ·        │
            │    record_error_code · escalate_to_technician ·                │
            │    flag_safety_concern · mark_resolved · add_note              │
            │ 3. Server runs the playbook: the next step's script is spoken  │
            │    WORD FOR WORD; the LLM's own words pass a guard that blocks │
            │    improvised repairs                                          │
            └────────────────────────────────────────────────────────────────┘
                    ▼
                  Sarvam bulbul:v3 (text-to-speech, voice "priya")
                    ▼
                  caller hears the step
```

**Why it can only say what the documents support.** Every physical instruction lives in `knowledge/playbooks/*.yaml` as a decision tree. Each step has `say.en` / `say.hi`, the allowed `outcomes`, and the `sources` it was paraphrased from (official LG India, Samsung India, Daikin India and Racold pages, listed in `knowledge/sources.yaml`). The LLM never writes instructions. It only classifies the caller's answer into one of the current step's outcomes, and the server rejects anything else:

- an outcome that isn't listed
- an answer to a step the caller hasn't heard yet
- a jump that isn't declared

The document library (`knowledge/docs` plus uploads) holds the source notes and any PDFs you add. It backs the citations. An optional doc-lookup tool for informational questions is off by default (`ENABLE_DOC_QA`).

**Safety.** Two independent screens run every turn:
1. A lexicon tuned for speech-to-text Hinglish, with negation handling. "spark nahi hai" stays quiet; "pata nahi, jalne ki smell aa rahi" triggers. It is also built not to fire on everyday phrases like "current aa raha hai", "jhatke maar rahi hai" (machine shaking) or "AC ki gas khatam".
2. The LLM's `flag_safety_concern` tool.

Either one ends troubleshooting, reads the script for that hazard, books an URGENT ticket and alerts the team. The gas script never asks the caller to touch a switch (it says: close the regulator, open the windows, go outside, call 1906). Electrical, fire, injury, battery and water-near-power each have their own script.

**Fallbacks.**
- An LLM error asks the caller to repeat; a second error books a callback ticket.
- If speech-to-text or text-to-speech dies mid-call, a callback ticket is booked.
- If the caller hangs up mid-playbook, they get a WhatsApp to resume.
- If they talk over the agent, the step they missed is asked again.

## Phone calls through Bolna

1. Put `ANTHROPIC_API_KEY` and `BOLNA_API_KEY` in `.env`, start the app, and expose it over https (for example `ngrok http 7860`).
2. Run:
   ```bash
   .venv\Scripts\python -m fixfirst bolna-setup --public-url https://YOUR-TUNNEL
   ```
   This registers `https://YOUR-TUNNEL/bolna/v1` as a Bolna custom LLM (protected by `FIXFIRST_LLM_KEY`), creates the agent (Sarvam saaras:v4 + bulbul:v3 "priya", Hindi welcome line, end-of-call webhook) and saves `BOLNA_AGENT_ID` to `.env`. Run it again after the tunnel URL changes.
3. Test from the Bolna dashboard, or have the agent call you:
   ```bash
   .venv\Scripts\python -m fixfirst bolna-call 98XXXXXXXX
   ```

How it fits together: Bolna sends the whole conversation on every turn; FixFirst reads the call id and phone numbers from the agent's prompt variables (`call_ref={execution_id} from={from_number} to={to_number}`), looks the caller up in RentBasket's API on the first turn, feeds only the caller's new words to the brain, and streams back the reply. If Bolna's history shows the last reply was cut short, the step is asked again. The webhook closes the call (follow-ups, recording link on the ticket); calls with no webhook close after 20 idle minutes.

## Project layout

| Path | What it is |
|---|---|
| `fixfirst/brain.py` | The agent: hazard screen → LLM tool choice → scripted playbook steps, guard, fallbacks, latency |
| `fixfirst/claude_llm.py` | Claude as the brain's model (Anthropic SDK; converts the brain's history to the Messages API) |
| `fixfirst/bolna.py` | Bolna custom-LLM bridge: maps Bolna turns to brain sessions, SSE streaming, webhook |
| `fixfirst/bolna_admin.py` | `bolna-setup` / `bolna-call` commands |
| `fixfirst/rentbasket.py` | Customer lookup from RentBasket's API by mobile number, copied into the local tables |
| `fixfirst/playbooks.py` | Loads, validates and runs playbooks; the error-code lookup |
| `fixfirst/safety.py` | Hazard screen and the unsafe-instruction guard |
| `fixfirst/voice.py` | Pipecat pipeline (Sarvam STT/TTS, Silero VAD), latency observer, voice-failure fallback |
| `fixfirst/server.py` | FastAPI pages and APIs, WebRTC offer, `/ws/exotel`, uploads, follow-up sender |
| `fixfirst/tickets.py` | Tickets, technician handoff text, WhatsApp messages, follow-ups, call records |
| `fixfirst/whatsapp.py` | Meta Cloud API sender with a dry-run outbox (same pattern as Reviews Analyzer) |
| `fixfirst/knowledge.py` | Document library (Markdown/PDF/TXT, BM25 search) |
| `fixfirst/eval.py` | Plays `tests/scenarios.yaml` against the real agent and prints the scorecard |
| `fixfirst/demo_llm.py` | Offline keyword brain for typed demos without a key |
| `knowledge/tenant.yaml` | Company settings: name, agent voice, support numbers, on-call, callback promise |
| `knowledge/playbooks/` | AC, washing machine (fully and semi-automatic), fridge, TV, inverter, water purifier, gas stove. The old geyser playbook is in `knowledge/archive/` (RentBasket doesn't rent geysers). |
| `knowledge/safety/hazards.yaml` | Safety scripts and the hazard lexicon |
| `knowledge/errorcodes/washer.yaml` | LG/Samsung washer codes from official pages only |
| `council/` | The design council's report and transcript |

## Going live, step by step

1. **Sarvam:** add `SARVAM_API_KEY`, then run `python -m fixfirst eval` (a simulated caller answers whatever the agent asks; `--scripted` replays fixed lines) and read `data/eval/report-*.json`. Keep `SARVAM_STT_MODE=codemix`: in "transcribe" mode English words come out in Devanagari and error codes get mangled. The eval is paced to about 50 requests a minute because the starter plan rate-limits. Make 10–20 real voice calls and watch voice-to-voice latency on `/ops`.
2. **WhatsApp:** create two utility templates with one body variable `{{1}}`, named `fixfirst_customer_update` and `fixfirst_team_ticket`. Then set `WHATSAPP_MODE=live`, the token and phone-number ID, and `team_whatsapp` numbers in `knowledge/tenant.yaml`. Set `PUBLIC_BASE_URL` to a public https URL so photo links open on phones.
3. **Phone (Exotel):** run behind https (for example `ngrok http 7860`). In Exotel App Bazaar, create Call Start → **Voicebot** applet with `wss://<host>/ws/exotel` → Hangup, and attach it to your ExoPhone. Callers are matched to customers by caller ID. The route follows Sarvam's Exotel + Pipecat guide but **has not been tested on a live number**.
4. **Supabase:** set `DATABASE_URL` to the Session pooler connection string. Tables are created on start in a separate `fixfirst` schema, so they stay out of Supabase's public REST API. Connected to RentBasket's project on 2026-10-24.
5. **Real customers and fleet:** replace `fixfirst/seed.py` with a sync from RentBasket's customer and rental records.

## Status of v1

Verified with the Sarvam key (2026-10-19):
- **Scorecard** (`python -m fixfirst eval`, simulated caller): fixable 10/10, escalations 10/10, safety 10/10, 0 guard blocks, median LLM time 0.25 s. The model's answers vary between runs, so run it more than once (see [docs/v1-review.md](docs/v1-review.md)).
- **Voice end to end:** a recorded Hindi caller was played into a real browser-style WebRTC call. The greeting played, saaras:v4 (codemix) transcribed it, the playbook started, and the agent answered. Voice-to-voice was **1.08 s** and **0.56 s** on two runs.
- 220 automated tests pass (no key needed): the safety screen including speech-to-text Devanagari transcripts, playbook validation, the brain with a scripted model, the Claude client, the RentBasket lookup, the Bolna bridge, the API and the fallbacks.
- Since 2026-10-24 (v1.1): on Claude (`claude-haiku-5-5`) the 36-scenario eval scores fixable 12/12, escalations 12/12, safety 12/12, 0 guard blocks, median brain time 1.1 s. Phone calls through Bolna and RentBasket lookup are built and unit-tested; no real Bolna call has run yet. See docs/v1-review.md.
- Typed calls, tickets, technician handoff, photo upload and the ops view work in the browser.

**Not yet verified:**
- Calls with real people (turn-taking, background noise, accents).
- The Exotel phone route.
- Live WhatsApp sending.
- Whether the playbook content is correct for RentBasket's actual models. Every playbook is `draft-unreviewed` until a senior technician signs off.

See [docs/v1-review.md](docs/v1-review.md) for the decisions, open questions and what to ask RentBasket.
