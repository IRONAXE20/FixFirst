"""Speech-to-text and text-to-speech for our own voice pipeline, from whichever provider has a key in the
environment. One Sarvam or ElevenLabs key covers both directions; Deepgram only listens (its voices don't
speak Hindi) and Cartesia only speaks reliably, so either needs a second provider for the other direction.

STT_PROVIDER / TTS_PROVIDER pick one explicitly; "auto" takes the first with a key, in the order below
(Sarvam first: it is built for Hindi and Hinglish on 8 kHz phone audio).
"""
from .config import Settings, Tenant

STT_ORDER = ("sarvam", "elevenlabs", "deepgram", "cartesia")
TTS_ORDER = ("sarvam", "elevenlabs", "cartesia")


def _has_key(settings: Settings, provider: str, *, tts: bool) -> bool:
    if provider == "sarvam":
        return settings.sarvam_ready
    if provider == "elevenlabs":
        return bool(settings.elevenlabs_api_key)
    if provider == "deepgram":
        return bool(settings.deepgram_api_key) and not tts
    if provider == "cartesia":
        return bool(settings.cartesia_api_key) and (not tts or bool(settings.cartesia_voice_id))
    return False


def pick(settings: Settings) -> tuple[str | None, str | None]:
    """(speech-to-text provider, text-to-speech provider), None where no key is set."""
    def choose(wanted: str, order: tuple, tts: bool) -> str | None:
        if wanted and wanted != "auto":
            return wanted if _has_key(settings, wanted, tts=tts) else None
        return next((p for p in order if _has_key(settings, p, tts=tts)), None)
    return choose(settings.stt_provider, STT_ORDER, False), choose(settings.tts_provider, TTS_ORDER, True)


def missing(settings: Settings) -> str | None:
    """Why a call can't run on our own voice pipeline yet, in one sentence (None: it can)."""
    stt, tts = pick(settings)
    if stt and tts:
        return None
    need = [what for what, have in (("speech-to-text", stt), ("text-to-speech", tts)) if not have]
    hint = ("SARVAM_API_KEY or ELEVENLABS_API_KEY covers both" if not stt and not tts else
            "add SARVAM_API_KEY, ELEVENLABS_API_KEY or CARTESIA_API_KEY + CARTESIA_VOICE_ID" if not tts else
            "add SARVAM_API_KEY, ELEVENLABS_API_KEY or DEEPGRAM_API_KEY")
    return f"No {' or '.join(need)} provider: {hint}"


def make_stt(settings: Settings, language: str = "hi"):
    """The Pipecat speech-to-text service. Callers mix Hindi and English, so each provider is set up for that."""
    from pipecat.transcriptions.language import Language
    provider, _ = pick(settings)
    if provider == "sarvam":
        from pipecat.services.sarvam.stt import SarvamSTTService
        return SarvamSTTService(api_key=settings.sarvam_api_key, mode=settings.stt_mode,
                                settings=SarvamSTTService.Settings(model=settings.stt_model,
                                                                   language="en-IN" if language == "en" else "hi-IN"))
    if provider == "elevenlabs":
        from pipecat.services.elevenlabs.stt import ElevenLabsRealtimeSTTService
        # No language: Scribe detects it per utterance, which suits Hinglish best.
        return ElevenLabsRealtimeSTTService(api_key=settings.elevenlabs_api_key)
    if provider == "deepgram":
        from pipecat.services.deepgram.stt import DeepgramSTTService
        # "multi": Nova-3's code-switching mode (Hindi and English in one sentence).
        return DeepgramSTTService(api_key=settings.deepgram_api_key,
                                  settings=DeepgramSTTService.Settings(model="nova-3", language="multi"))
    if provider == "cartesia":
        from pipecat.services.cartesia.stt import CartesiaSTTService
        return CartesiaSTTService(api_key=settings.cartesia_api_key,
                                  settings=CartesiaSTTService.Settings(language=Language.HI))
    raise RuntimeError(missing(settings))


def make_tts(settings: Settings, tenant: Tenant, language: str = "hi"):
    """The Pipecat text-to-speech service, speaking Hindi with English words (the agent's replies)."""
    from pipecat.transcriptions.language import Language
    _, provider = pick(settings)
    if provider == "sarvam":
        from pipecat.services.sarvam.tts import SarvamTTSService
        return SarvamTTSService(api_key=settings.sarvam_api_key,
                                settings=SarvamTTSService.Settings(model=settings.tts_model, voice=tenant.tts_speaker,
                                                                   language="en-IN" if language == "en" else "hi-IN"))
    if provider == "elevenlabs":
        from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
        # Without ELEVENLABS_VOICE_ID this is ElevenLabs' default voice; pick a Hindi one from the Voice Library.
        return ElevenLabsTTSService(api_key=settings.elevenlabs_api_key,
                                    settings=ElevenLabsTTSService.Settings(
                                        model=settings.elevenlabs_tts_model, language=Language.HI,
                                        **({"voice": settings.elevenlabs_voice_id} if settings.elevenlabs_voice_id else {})))
    if provider == "cartesia":
        from pipecat.services.cartesia.tts import CartesiaTTSService
        return CartesiaTTSService(api_key=settings.cartesia_api_key,
                                  settings=CartesiaTTSService.Settings(model=settings.cartesia_tts_model,
                                                                       voice=settings.cartesia_voice_id,
                                                                       language=Language.HI))
    raise RuntimeError(missing(settings))
