"""Real-time voice: Pipecat pipeline with Sarvam speech-to-text and text-to-speech around
the FixFirst brain. The same `run_bot` serves the browser (WebRTC) and phone calls
(Exotel websocket); only the transport differs."""
import asyncio
import time

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (BotStartedSpeakingFrame, ErrorFrame, InterimTranscriptionFrame, MetricsFrame,
                                   TTSSpeakFrame, UserStoppedSpeakingFrame)
from pipecat.metrics.metrics import TTFBMetricsData
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (LLMContextAggregatorPair,
                                                                    LLMUserAggregatorParams)
from pipecat.services.sarvam.llm import SarvamLLMService
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.sarvam.tts import SarvamTTSService

from .brain import Brain
from .config import Settings, Tenant

LANG_CODES = {"hi": "hi-IN", "en": "en-IN"}


class FixFirstLLMService(SarvamLLMService):
    """Sits in Pipecat's LLM slot but hands each completed user turn to the FixFirst brain,
    which runs its own safety screen, tool loop and scripted playbook steps."""

    def __init__(self, brain: Brain, **kwargs):
        super().__init__(**kwargs)
        self.brain = brain

    async def _process_context(self, context: LLMContext):
        text = _latest_user_text(context)
        if not text:
            return
        presented_before = set(self.brain.s.presented)
        turn = asyncio.ensure_future(self.brain.respond(text))
        try:
            # Shielded so the brain's state stays consistent if the caller interrupts.
            result = await asyncio.shield(turn)
        except asyncio.CancelledError:
            turn.add_done_callback(lambda _t: self._mark_unheard(presented_before))
            raise
        if result.speech:
            await self._push_llm_text(result.speech)

    def _mark_unheard(self, presented_before: set[str]) -> None:
        """The caller talked over this turn, so its new step was not heard; it will be asked again."""
        self.brain.s.presented = set(presented_before)
        self.brain.s.publish("interrupted")


def _latest_user_text(context: LLMContext) -> str:
    for msg in reversed(context.get_messages()):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return " ".join(p.get("text", "") for p in content if isinstance(p, dict)).strip()
    return ""


class CallObserver(BaseObserver):
    """Measures voice-to-voice latency (caller stops speaking -> agent starts speaking),
    collects per-service time-to-first-byte, and streams interim transcripts to the UI."""

    def __init__(self, brain: Brain):
        super().__init__(observe_every_push=False)
        self.brain = brain
        self.user_stopped_at: float | None = None
        self.ttfb: dict[str, int] = {}
        self.failed = False

    async def on_push_frame(self, data: FramePushed):
        frame = data.frame
        s = self.brain.s
        if isinstance(frame, UserStoppedSpeakingFrame):
            self.user_stopped_at = time.monotonic()
            self.ttfb = {}
        elif isinstance(frame, InterimTranscriptionFrame):
            s.publish("user_partial", text=frame.text)
        elif isinstance(frame, ErrorFrame):
            source = type(frame.processor).__name__ if frame.processor else "pipeline"
            s.publish("error", message=f"{source}: {frame.error}"[:300])
            if not self.failed and ("STT" in source or "TTS" in source):
                # Speech in or out is down: the caller can't be helped by voice, so hand off to a person.
                self.failed = True
                asyncio.create_task(self.brain.voice_failure(f"{source} failed: {frame.error}"[:200]))
        elif isinstance(frame, MetricsFrame):
            for d in frame.data:
                if isinstance(d, TTFBMetricsData) and d.value:
                    name = d.processor.lower()
                    key = "stt_ms" if "stt" in name else "tts_ms" if "tts" in name else None
                    if key:
                        self.ttfb[key] = int(d.value * 1000)
        elif isinstance(frame, BotStartedSpeakingFrame) and self.user_stopped_at:
            v2v = int((time.monotonic() - self.user_stopped_at) * 1000)
            self.user_stopped_at = None
            entry = {"voice_to_voice_ms": v2v, **self.ttfb}
            if s.latency:
                s.latency[-1].update(entry)
            s.publish("latency", **(s.latency[-1] if s.latency else entry))


async def run_bot(transport, brain: Brain, settings: Settings, tenant: Tenant, *,
                  sample_rate_in: int | None = None, sample_rate_out: int | None = None) -> None:
    lang = LANG_CODES.get(brain.s.language, "hi-IN")
    stt = SarvamSTTService(
        api_key=settings.sarvam_api_key,
        settings=SarvamSTTService.Settings(model=settings.stt_model, language=lang),
        mode=settings.stt_mode,
    )
    tts = SarvamTTSService(
        api_key=settings.sarvam_api_key,
        settings=SarvamTTSService.Settings(model=settings.tts_model, voice=tenant.tts_speaker, language=lang),
    )
    llm = FixFirstLLMService(
        brain,
        api_key=settings.sarvam_api_key,
        settings=SarvamLLMService.Settings(model=settings.llm_model),
    )
    context = LLMContext([])
    aggregators = LLMContextAggregatorPair(
        context, user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()))

    pipeline = Pipeline([
        transport.input(),
        stt,
        aggregators.user(),
        llm,
        tts,
        transport.output(),
        aggregators.assistant(),
    ])
    params = PipelineParams(enable_metrics=True)
    if sample_rate_in:
        params.audio_in_sample_rate = sample_rate_in
    if sample_rate_out:
        params.audio_out_sample_rate = sample_rate_out
    # Callers may go quiet for minutes while cleaning a filter, so allow a long idle window.
    task = PipelineWorker(pipeline, params=params, observers=[CallObserver(brain)], enable_rtvi=False,
                          idle_timeout_secs=900)

    @transport.event_handler("on_client_connected")
    async def on_connected(_transport, _client):
        logger.info(f"[{brain.s.id}] caller connected ({brain.s.channel})")
        brain.s.publish("connected")
        await task.queue_frames([TTSSpeakFrame(brain.greeting())])

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(_transport, _client):
        logger.info(f"[{brain.s.id}] caller disconnected")
        await brain.on_hangup()
        await task.cancel()

    await PipelineRunner(handle_sigint=False).run(task)
