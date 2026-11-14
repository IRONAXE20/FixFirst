"""Voice bake-off for the Hinglish phone line: which text-to-speech voice and which speech-to-text model to use
once FixFirst runs its own voice pipeline instead of Bolna.

    python -m fixfirst.voice_bakeoff [--calls 8]

1. Downloads the latest Bolna call recordings (stereo, 8 kHz: agent and caller on separate channels) and cuts the
   caller's channel into one clip per thing they said.
2. For every provider with a key in .env, speaks the agent's real lines at phone quality (8 kHz) and transcribes
   the caller clips. Keys: SARVAM_API_KEY, ELEVENLABS_API_KEY (+ ELEVENLABS_VOICE_ID), CARTESIA_API_KEY
   (+ CARTESIA_VOICE_ID), DEEPGRAM_API_KEY. Providers without a key are skipped; finished files are kept, so a
   rerun after adding a key only does the new provider.
3. Writes data/voice-bakeoff/index.html: every line in every voice side by side, every clip with each
   transcript, and how long each request took.

Everything stays in data/ (git-ignored): the recordings are real calls.
"""
import argparse
import audioop
import base64
import html
import io
import json
import os
import time
import wave
from pathlib import Path

import httpx
import numpy as np
from dotenv import load_dotenv

from .config import Settings

OUT = Path("data/voice-bakeoff")
RATE = 8000   # phone audio

# The agent's lines from the FF-1035 demo call, plus the safety and handoff lines: what callers actually hear.
LINES = [
    "नमस्ते! मैं RentBasket की FixFirst service assistant बोल रही हूँ। आपकी क्या मदद कर सकती हूँ?",
    "ये अक्सर घर पर ही ठीक हो जाता है। मैं आपको step by step बताती हूँ। क्या आप अभी AC के पास हैं?",
    "पहले remote से AC बंद कीजिए, और फिर wall switch से। बंद हो जाए तो बताइए।",
    "अब indoor unit का front panel खोलिए और filters को धीरे से बाहर निकालिए। निकल जाएँ तो बताइए।",
    "ठीक है, आराम से कीजिए, हो जाए तो बताइए।",
    "बहुत बढ़िया! मैं ticket close कर रही हूँ। कल हम WhatsApp पर check करेंगे। और कुछ मदद चाहिए?",
    "मैंने आपका ticket raise कर दिया है। हमारे technician जल्दी आपसे contact करेंगे। आपका ticket number है F F 1 0 3 5।",
    "कृपया तुरंत gas का regulator बंद कीजिए, कोई switch मत दबाइए, और खिड़कियाँ खोल दीजिए।",
    "मैं आपको हमारी service team से connect कर रही हूँ।",
    "Thank you Ananya जी। RentBasket choose करने के लिए शुक्रिया। Have a good day!",
]


# ---- audio helpers ---------------------------------------------------------------------------
def wav_bytes(pcm: bytes, rate: int = RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def to_phone(audio: bytes) -> bytes:
    """Any WAV (or raw 16-bit 16 kHz PCM) -> 8 kHz mono WAV, as a phone line would carry it."""
    if audio[:4] == b"RIFF":
        with wave.open(io.BytesIO(audio)) as w:
            rate, ch, width, pcm = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.readframes(w.getnframes())
    else:
        rate, ch, width, pcm = 16000, 1, 2, audio
    if width != 2:
        pcm, width = audioop.lin2lin(pcm, width, 2), 2
    if ch == 2:
        pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
    if rate != RATE:
        pcm, _ = audioop.ratecv(pcm, 2, 1, rate, RATE, None)
    return wav_bytes(pcm)


def decode_stereo(mp3: bytes) -> np.ndarray:
    """MP3 -> int16 array of shape (2, samples) at 8 kHz."""
    import av
    chunks = []
    with av.open(io.BytesIO(mp3)) as f:
        resampler = av.AudioResampler(format="s16p", layout="stereo", rate=RATE)
        for frame in f.decode(audio=0):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray())
    return np.concatenate(chunks, axis=1).astype(np.int16)


def speech_segments(x: np.ndarray, frame: int = 160) -> list[tuple[int, int]]:
    """(start, end) samples of each stretch of speech: 20 ms frames above an energy floor, gaps under 0.7 s
    joined, pieces under 0.5 s dropped, nothing over 25 s (the REST speech-to-text limit is 30 s)."""
    n = len(x) // frame
    rms = np.sqrt((x[: n * frame].astype(np.float32).reshape(n, frame) ** 2).mean(axis=1))
    floor = max(250.0, 4 * float(np.median(rms)))
    voiced = rms > floor
    segs, start, quiet = [], None, 0
    for i, v in enumerate(voiced):
        if v:
            start, quiet = (i if start is None else start), 0
        elif start is not None:
            quiet += 1
            if quiet > 35:
                segs.append((start, i - quiet + 1))
                start, quiet = None, 0
    if start is not None:
        segs.append((start, n))
    out = []
    for a, b in segs:
        while b - a > 1250:   # 25 s
            out.append((a, a + 1250))
            a += 1250
        if b - a >= 25:       # 0.5 s
            out.append((a, b))
    return [(a * frame, b * frame) for a, b in out]


# ---- 1. caller clips from real calls ------------------------------------------------------------
def recording_urls(count: int) -> list[str]:
    """The latest calls' Bolna recordings, from the "Call recording: <url>" note on their tickets (Bolna's own
    execution list comes back empty for older calls)."""
    from sqlalchemy import desc, select

    from .db import get_engine, tickets
    urls: list[str] = []
    with get_engine().connect() as c:
        for (summary,) in c.execute(select(tickets.c.summary).order_by(desc(tickets.c.id)).limit(100)):
            for note in (summary or {}).get("notes") or []:
                if note.startswith("Call recording: ") and note[16:] not in urls:
                    urls.append(note[16:])
            if len(urls) >= count:
                break
    return urls[:count]


def fetch_calls(settings: Settings, count: int) -> list[Path]:
    clips_dir = OUT / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    clips = sorted(clips_dir.glob("*.wav"))
    if clips:
        return clips
    for url in recording_urls(count):
        mp3 = httpx.get(url, headers={"Authorization": f"Bearer {settings.bolna_api_key}"}, timeout=60,
                        follow_redirects=True).content
        x = decode_stereo(mp3)
        # The agent speaks first (the welcome line): the channel with speech in its first 3 s is the agent.
        first = [np.abs(ch[: 3 * RATE]).mean() for ch in x]
        caller = x[1 if first[0] > first[1] else 0]
        call = url.rstrip("/").rsplit("/", 1)[-1][:8]
        for i, (a, b) in enumerate(speech_segments(caller)):
            (clips_dir / f"{call}-{i:02d}.wav").write_bytes(wav_bytes(caller[a:b].tobytes()))
    return sorted(clips_dir.glob("*.wav"))


# ---- 2a. text-to-speech -----------------------------------------------------------------------
def tts_providers() -> dict:
    out = {}
    if os.getenv("SARVAM_API_KEY"):
        for speaker in (os.getenv("BAKEOFF_SARVAM_SPEAKERS") or "priya").split(","):
            out[f"sarvam-{speaker.strip()}"] = lambda text, sp=speaker.strip(): _sarvam_tts(text, sp)
    if os.getenv("ELEVENLABS_API_KEY"):
        out["elevenlabs-flash"] = _eleven_tts
    if os.getenv("CARTESIA_API_KEY") and os.getenv("CARTESIA_VOICE_ID"):
        out["cartesia-sonic"] = _cartesia_tts
    return out


def _sarvam_tts(text: str, speaker: str) -> bytes:
    r = httpx.post("https://api.sarvam.ai/text-to-speech", timeout=60,
                   headers={"api-subscription-key": os.environ["SARVAM_API_KEY"]},
                   json={"text": text, "target_language_code": "hi-IN", "model": os.getenv("SARVAM_TTS_MODEL", "bulbul:v3"),
                         "speaker": speaker, "speech_sample_rate": RATE})
    r.raise_for_status()
    return base64.b64decode(r.json()["audios"][0])


def _eleven_tts(text: str) -> bytes:
    voice = os.getenv("ELEVENLABS_VOICE_ID") or "21m00Tcm4TlvDq8ikWAM"   # default voice; set a Hindi one
    r = httpx.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}", timeout=60,
                   params={"output_format": "pcm_16000"}, headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
                   json={"text": text, "model_id": os.getenv("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5"),
                         "language_code": "hi"})
    r.raise_for_status()
    return r.content   # raw 16 kHz PCM


def _cartesia_tts(text: str) -> bytes:
    r = httpx.post("https://api.cartesia.ai/tts/bytes", timeout=60,
                   headers={"X-API-Key": os.environ["CARTESIA_API_KEY"],
                            "Cartesia-Version": os.getenv("CARTESIA_VERSION", "2026-03-01")},
                   json={"model_id": os.getenv("CARTESIA_MODEL", "sonic-3"), "transcript": text, "language": "hi",
                         "voice": {"mode": "id", "id": os.environ["CARTESIA_VOICE_ID"]},
                         "output_format": {"container": "wav", "encoding": "pcm_s16le", "sample_rate": RATE}})
    r.raise_for_status()
    return r.content


# ---- 2b. speech-to-text -----------------------------------------------------------------------
def stt_providers() -> dict:
    out = {}
    if os.getenv("SARVAM_API_KEY"):
        out["sarvam"] = _sarvam_stt
    if os.getenv("ELEVENLABS_API_KEY"):
        out["elevenlabs-scribe"] = _eleven_stt
    if os.getenv("DEEPGRAM_API_KEY"):
        out["deepgram-nova3"] = _deepgram_stt
    return out


def _sarvam_stt(audio: bytes) -> str:
    r = httpx.post("https://api.sarvam.ai/speech-to-text", timeout=60,
                   headers={"api-subscription-key": os.environ["SARVAM_API_KEY"]},
                   files={"file": ("clip.wav", audio, "audio/wav")},
                   data={"model": os.getenv("SARVAM_STT_MODEL", "saaras:v3"),
                         "mode": os.getenv("SARVAM_STT_MODE", "transcribe"), "language_code": "hi-IN"})
    r.raise_for_status()
    return r.json().get("transcript", "")


def _eleven_stt(audio: bytes) -> str:
    r = httpx.post("https://api.elevenlabs.io/v1/speech-to-text", timeout=60,
                   headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
                   files={"file": ("clip.wav", audio, "audio/wav")},
                   data={"model_id": os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2")})
    r.raise_for_status()
    return r.json().get("text", "")


def _deepgram_stt(audio: bytes) -> str:
    r = httpx.post("https://api.deepgram.com/v1/listen", timeout=60, content=audio,
                   params={"model": "nova-3", "language": "multi", "smart_format": "true"},
                   headers={"Authorization": f"Token {os.environ['DEEPGRAM_API_KEY']}", "Content-Type": "audio/wav"})
    r.raise_for_status()
    return r.json()["results"]["channels"][0]["alternatives"][0]["transcript"]


# ---- 3. run and report ------------------------------------------------------------------------
def run(count: int) -> Path:
    load_dotenv()
    settings = Settings.from_env()
    results_file = OUT / "results.json"
    OUT.mkdir(parents=True, exist_ok=True)
    results = json.loads(results_file.read_text(encoding="utf-8")) if results_file.exists() else {"tts": {}, "stt": {}}

    for name, speak in tts_providers().items():
        folder = OUT / "tts" / name
        folder.mkdir(parents=True, exist_ok=True)
        for i, text in enumerate(LINES):
            key, path = f"{name}/{i:02d}", folder / f"{i:02d}.wav"
            if path.exists() and key in results["tts"]:
                continue
            t0 = time.monotonic()
            try:
                path.write_bytes(to_phone(speak(text)))
                results["tts"][key] = {"ms": int((time.monotonic() - t0) * 1000)}
            except Exception as e:   # one provider's failure never stops the rest
                results["tts"][key] = {"error": _error(e)}
            print("tts", key, results["tts"][key])

    clips = fetch_calls(settings, count) if settings.bolna_api_key else sorted((OUT / "clips").glob("*.wav"))
    for name, hear in stt_providers().items():
        for clip in clips:
            key = f"{name}/{clip.stem}"
            if key in results["stt"] and "text" in results["stt"][key]:
                continue
            t0 = time.monotonic()
            try:
                results["stt"][key] = {"text": hear(clip.read_bytes()), "ms": int((time.monotonic() - t0) * 1000)}
            except Exception as e:
                results["stt"][key] = {"error": _error(e)}
            print("stt", key, results["stt"][key])

    results_file.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    page = OUT / "index.html"
    page.write_text(_page(results, clips), encoding="utf-8")
    return page


def _error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}: {e.response.text[:200]}"
    return f"{type(e).__name__}: {e}"[:200]


def _page(results: dict, clips: list[Path]) -> str:
    esc = html.escape
    voices = sorted({k.split("/")[0] for k in results["tts"]})
    hearers = sorted({k.split("/")[0] for k in results["stt"]})

    def cell(kind, key, body):
        r = results[kind].get(key)
        if not r:
            return "<td class=muted>–</td>"
        if "error" in r:
            return f"<td class=err>{esc(r['error'])}</td>"
        return f"<td>{body(r)}<div class=ms>{r['ms']} ms</div></td>"

    tts_rows = "".join(
        f"<tr><td class=line>{esc(text)}</td>" + "".join(
            cell("tts", f"{v}/{i:02d}", lambda r, v=v, i=i: f"<audio controls preload=none src='tts/{v}/{i:02d}.wav'></audio>")
            for v in voices) + "</tr>" for i, text in enumerate(LINES))
    stt_rows = "".join(
        f"<tr><td><audio controls preload=none src='clips/{c.name}'></audio><div class=ms>{esc(c.stem)}</div></td>" + "".join(
            cell("stt", f"{h}/{c.stem}", lambda r: esc(r["text"]) or "<span class=muted>(nothing)</span>")
            for h in hearers) + "</tr>" for c in clips)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Voice bake-off</title><style>
body{{font:14px/1.5 -apple-system,"Segoe UI",Roboto,sans-serif;margin:0;padding:24px 16px;color:#1f2937;background:#fff}}
main{{max-width:1200px;margin:0 auto}} h1{{font-size:20px}} h2{{font-size:16px;margin-top:32px}}
table{{border-collapse:collapse;width:100%}} th,td{{border:1px solid #e5e7eb;padding:8px;vertical-align:top;text-align:left}}
th{{background:#f9fafb}} .line{{max-width:360px}} audio{{width:220px;height:32px}} .ms{{color:#6b7280;font-size:12px}}
.muted{{color:#9ca3af}} .err{{color:#b91c1c;font-size:12px;max-width:260px}} .wrap{{overflow-x:auto}}
</style></head><body><main>
<h1>Voice bake-off: Hinglish phone line</h1>
<p class=muted>All voices are at 8 kHz, as a caller hears them on a phone line. Times are for the whole request
(not the first audio), so compare them with each other, not with streaming figures.</p>
<h2>Text-to-speech: the agent's real lines</h2><div class=wrap><table><tr><th>Line</th>{''.join(f'<th>{esc(v)}</th>' for v in voices)}</tr>{tts_rows}</table></div>
<h2>Speech-to-text: callers on real calls</h2><div class=wrap><table><tr><th>Caller clip</th>{''.join(f'<th>{esc(h)}</th>' for h in hearers)}</tr>{stt_rows}</table></div>
</main></body></html>"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--calls", type=int, default=8, help="how many recent call recordings to cut into caller clips")
    print("Wrote", run(ap.parse_args().calls))
