# Our own voice line (without Bolna)

FixFirst can run phone calls itself: **Plivo** carries the call (the same carrier Bolna used underneath), our
server runs the voice pipeline (Pipecat), and **one speech provider** listens and speaks. The brain, tickets,
WhatsApp messages, /ops and the tickets API stay exactly the same as on Bolna.

```
Caller ──▶ Plivo number ──▶ /plivo/answer (record the call, stream the audio)
                              │
                              ▼  wss://…/ws/plivo
        speech-to-text ──▶ FixFirst brain ──▶ text-to-speech ──▶ back to the caller
        (from your key)    (same as today)     (from your key)
```

## 1. A speech key

Put **one** of these in Render's Environment tab (and in `.env` for local tests):

| Key | Listens | Speaks | Notes |
|---|---|---|---|
| `SARVAM_API_KEY` | ✅ | ✅ | Built for Hindi/Hinglish on phone audio. Voice: `tts_speaker` in `knowledge/tenant.yaml` (now `priya`) |
| `ELEVENLABS_API_KEY` | ✅ | ✅ | Also set `ELEVENLABS_VOICE_ID` to a Hindi voice from the Voice Library (otherwise a default voice) |
| `CARTESIA_API_KEY` + `CARTESIA_VOICE_ID` | ✅ | ✅ | Hindi listening is less proven; a Hindi voice ID is required |
| `DEEPGRAM_API_KEY` | ✅ | ❌ | Its voices don't speak Hindi: pair it with one of the above for speaking |

With several keys, Sarvam is used first. `STT_PROVIDER` / `TTS_PROVIDER` (`sarvam`, `elevenlabs`, `cartesia`,
`deepgram`) choose explicitly, e.g. Deepgram to listen and ElevenLabs to speak.

## 2. A Plivo number

1. Create a Plivo account, complete the India KYC, and buy an Indian number (voice).
2. Put `PLIVO_AUTH_ID` and `PLIVO_AUTH_TOKEN` (Plivo console, Overview) on Render and in `.env`.
3. Point the number at FixFirst:

```bash
python -m fixfirst plivo-setup --public-url https://fixfirst-zgo0.onrender.com --number 91XXXXXXXXXX
```

## 3. Test, then switch

1. Call the Plivo number. Check /ops: the call, its transcript, the ticket and the recording on it.
   `python -m fixfirst check` shows which speech providers and phone line are active; the /ops header too.
2. Ask for a person (9am–8pm): the call moves to the care number; /ops shows "Transferred" or "Transfer failed".
3. When it's right, forward RentBasket's customer line to the Plivo number and unassign the number in Bolna.
   Bolna can stay configured as a fallback until then: both lines work at the same time.

## What it does on a call

- Greets the caller (by name when RentBasket knows the number), listens and replies in Hinglish.
- A reply the caller talks over is handled the same way as on Bolna (repeated, recapped, or merged with their
  next words).
- Hangs up after the goodbye; a caller silent for 5 minutes is disconnected.
- 9am–8pm handoff: "connect कर रही हूँ", then Plivo dials the care number; the result is saved on the call.
- Records the call; the recording link goes on the ticket (and into the tickets API).
- If the speech provider fails mid-call, a callback ticket is raised.

**Not yet verified on a live call:** all of the above runs in tests and a local smoke test (fake Plivo call,
dummy key). The first real call with a real key and a Plivo number is the live check.
