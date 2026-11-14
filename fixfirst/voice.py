"""Real-time voice: a Pipecat pipeline around the FixFirst brain, with speech-to-text and text-to-speech from
whichever provider has a key (speech.py). The same `run_bot` serves the browser (WebRTC) and phone calls
(Plivo, Exotel); only the transport differs. It does what Bolna did for us: turn-taking, the greeting, a reply
the caller talked over (turns.py), hanging up after the goodbye and the transfer to customer care."""
import asyncio
import time

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (BotStartedSpeakingFrame, BotStoppedSpeakingFrame, EndWorkerFrame, ErrorFrame,
                                   Frame, InterimTranscriptionFrame, InterruptionFrame, LLMContextFrame,
                                   LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame, MetricsFrame,
                                   TTSSpeakFrame, UserStoppedSpeakingFrame)
from pipecat.metrics.metrics import TTFBMetricsData
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (LLMContextAggregatorPair,
                                                                    LLMUserAggregatorParams)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from .brain import Brain
from .config import Settings, Tenant
from .speech import make_stt, make_tts
from .turns import TurnKeeper

IDLE_SECONDS = 300   # a caller cleaning a filter may go quiet for minutes; after this the call ends


class FixFirstProcessor(FrameProcessor):
    """Sits where an LLM would in the pipeline and hands each completed caller turn to the FixFirst brain, which
    runs its own safety screen, tool loop and scripted playbook steps. `telephony` (optional) can transfer the
    call to a person: `await telephony.transfer()`."""

    def __init__(self, brain: Brain, telephony=None, **kwargs):
        super().__init__(**kwargs)
        self.brain = brain
        self.telephony = telephony
        self.keeper = TurnKeeper(brain)
        self._lock = asyncio.Lock()
        self._speaking = False        # our reply is queued or playing
        self._heard = True            # the caller heard all of our last reply
        self._transfer_pending = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            text = _latest_user_text(frame.context)
            if text:
                await self._answer(text)
            return
        if isinstance(frame, BotStoppedSpeakingFrame) and self._speaking:
            self._speaking = False
            await self._after_speech()
        elif isinstance(frame, InterruptionFrame) and self._speaking:
            # The caller talked over us: the next turn decides what to do with the unheard reply.
            self._speaking, self._heard = False, False
            await self._after_speech()
        await self.push_frame(frame, direction)

    async def _answer(self, text: str) -> None:
        heard, self._heard = self._heard, True
        turn = asyncio.ensure_future(self._locked_answer(text, heard))
        try:
            # Shielded: if the caller interrupts while the brain is thinking, the turn still finishes (its state
            # stays consistent) and the reply counts as unheard.
            speech = await asyncio.shield(turn)
        except asyncio.CancelledError:
            self._heard = False
            raise
        s = self.brain.s
        if speech:
            self._speaking = True
            await self.push_frame(LLMFullResponseStartFrame())
            await self.push_frame(LLMTextFrame(speech))
            await self.push_frame(LLMFullResponseEndFrame())
        if s.end_requested:
            # After the goodbye plays out, end the call (the phone serializer hangs up).
            await self.push_frame(EndWorkerFrame(reason="goodbye"))
        elif s.transfer_requested and self.telephony:
            self._transfer_pending = True
            if not speech:
                await self._after_speech()

    async def _locked_answer(self, text: str, heard: bool) -> str:
        async with self._lock:
            return await self.keeper.answer(text, heard=heard)

    async def _after_speech(self) -> None:
        """"Connect कर रही हूँ" has played (or the caller cut it off): move the call to customer care."""
        if not self._transfer_pending:
            return
        s = self.brain.s
        self._transfer_pending, s.transfer_requested = False, False
        s.transfers_asked += 1
        s.publish("transfer")
        try:
            await self.telephony.transfer()
        except Exception as e:   # the call stays with us; the handoff ticket already asks for a callback
            logger.error(f"[{s.id}] transfer failed: {e}")
            from .turns import note_transfer
            note_transfer(s, "failed: transfer error", self.brain.tickets)


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


async def run_bot(transport, brain: Brain, settings: Settings, tenant: Tenant, *, telephony=None,
                  sample_rate_in: int | None = None, sample_rate_out: int | None = None) -> None:
    stt = make_stt(settings, brain.s.language)
    tts = make_tts(settings, tenant, brain.s.language)
    fixfirst = FixFirstProcessor(brain, telephony)
    context = LLMContext([])
    aggregators = LLMContextAggregatorPair(
        context, user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()))

    pipeline = Pipeline([
        transport.input(),
        stt,
        aggregators.user(),
        fixfirst,
        tts,
        transport.output(),
        aggregators.assistant(),
    ])
    params = PipelineParams(enable_metrics=True)
    if sample_rate_in:
        params.audio_in_sample_rate = sample_rate_in
    if sample_rate_out:
        params.audio_out_sample_rate = sample_rate_out
    task = PipelineWorker(pipeline, params=params, observers=[CallObserver(brain)], enable_rtvi=False,
                          idle_timeout_secs=IDLE_SECONDS)

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
