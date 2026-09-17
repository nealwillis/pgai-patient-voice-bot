"""The patient: a pipeline voice agent that calls the office and role-plays a caller.

Pipeline mode only -- separate STT, LLM and TTS. No realtime/speech-to-speech model.

    Deepgram nova-3  ->  Claude Haiku 4.5  ->  Cartesia Sonic
    turn-taking: Silero VAD + LiveKit's turn detector model

The agent places the outbound call itself (ctx.add_sip_participant) rather than
having the runner dial separately, so there is no race between the call being
answered and the agent being ready to listen.
"""

from __future__ import annotations

import asyncio
import json
import logging

from livekit import agents
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    RecordingOptions,
    RunContext,
    TurnHandlingOptions,
    function_tool,
    get_job_context,
    inference,
)
# All plugins must be imported here, on the main thread. LiveKit registers a
# plugin at import time and rejects registration from a job thread, so a lazy
# `from livekit.plugins import openai` inside a builder raises
# "Plugins must be registered on the main thread" only on a real call -- which
# is why preflight, running on the main thread, could not catch it.
from livekit.plugins import anthropic, cartesia, deepgram, elevenlabs, openai, silero

from .artifacts import save_artifacts
from .latency import LatencyLog
from .config import (
    ANTHROPIC_MODEL,
    CARTESIA_MODEL,
    DEEPGRAM_MODEL,
    ENDPOINTING_MAX_DELAY,
    ENDPOINTING_MODE,
    ENDPOINTING_MIN_DELAY,
    INTERRUPTION_MIN_DURATION,
    INTERRUPTION_MIN_WORDS,
    LLM_CACHING,
    LLM_PROVIDER,
    MAX_CALL_SECONDS,
    OPENAI_MODEL,
    PREEMPTIVE_GENERATION,
    STT_PROVIDER,
    TARGET_NUMBER,
    TTS_PROVIDER,
    TURN_DETECTOR,
    VAD_MIN_SILENCE,
    assert_allowed_number,
    env,
    require_env,
)
from .scenarios import Scenario, load_scenario

logger = logging.getLogger("patient")

AGENT_NAME = "pgai-patient"


# --------------------------------------------------------------------------
# provider wiring -- each branch is a Phase 2 A/B switch, set via .env
# --------------------------------------------------------------------------
def _build_stt(scenario: Scenario | None = None):
    # A non-English persona means the conversation is bilingual: we speak their
    # language, the far end most likely answers in English. Multilingual STT keeps
    # us understanding them either way.
    lang_multi = bool(scenario and scenario.language != "en")
    if STT_PROVIDER == "livekit":
        # The same Deepgram model, routed through LiveKit's inference gateway and
        # billed against LiveKit credits -- no Deepgram account needed. Costs one
        # extra network hop, so prefer the direct client when a key is available.
        return inference.STT(
            model=f"deepgram/{DEEPGRAM_MODEL}",
            language="multi" if lang_multi else "en",
        )
    # filler_words keeps the receptionist's "um"s in the transcript, which is
    # evidence we care about when judging its pacing.
    return deepgram.STT(
        model=DEEPGRAM_MODEL,
        language="multi" if lang_multi else "en-US",
        filler_words=True,
    )


def _build_llm():
    if LLM_PROVIDER == "openai":
        return openai.LLM(model=OPENAI_MODEL, temperature=0.8)
    # max_tokens is a hard brake on rambling: a real caller says one or two
    # sentences, and a long turn is the clearest tell that a bot is talking.
    kwargs = {"model": ANTHROPIC_MODEL, "temperature": 0.9, "max_tokens": 60}
    if LLM_CACHING:
        kwargs["caching"] = "ephemeral"
    return anthropic.LLM(**kwargs)


def _build_tts(scenario: Scenario | None = None):
    override = (scenario.voice if scenario else None) or ""
    if TTS_PROVIDER == "elevenlabs":
        return elevenlabs.TTS(
            voice_id=override or require_env("ELEVENLABS_VOICE_ID"),
            model="eleven_flash_v2_5",
        )
    voice = override or env("CARTESIA_VOICE_ID")
    kwargs = {"model": CARTESIA_MODEL, "language": scenario.language if scenario else "en"}
    if voice:
        kwargs["voice"] = voice
    return cartesia.TTS(**kwargs)


def _build_turn_detection():
    if TURN_DETECTOR == "local":
        # Runs on-device (ONNX, no per-minute cost). Deprecated upstream in
        # favour of the hosted audio model, kept as a zero-cost fallback.
        from livekit.plugins.turn_detector.english import EnglishModel

        return EnglishModel()
    # Audio-based: predicts end-of-turn from acoustics as well as text, which
    # handles trailing-off and mid-sentence pauses better than the text model.
    return inference.TurnDetector()


def build_session(scenario: Scenario, vad=None) -> AgentSession:
    """Build the pipeline session for a scenario.

    Lives here rather than inline in the entrypoint so that preflight.py can
    construct the real thing -- a bad turn-handling key then fails for free
    instead of on a paid call.
    """
    return AgentSession(
        stt=_build_stt(scenario),
        llm=_build_llm(),
        tts=_build_tts(scenario),
        vad=vad or silero.VAD.load(min_silence_duration=VAD_MIN_SILENCE),
        turn_handling=TurnHandlingOptions(
            turn_detection=_build_turn_detection(),
            endpointing={
                "mode": ENDPOINTING_MODE,
                "min_delay": ENDPOINTING_MIN_DELAY,
                "max_delay": ENDPOINTING_MAX_DELAY,
            },
            preemptive_generation={
                "enabled": PREEMPTIVE_GENERATION,
                "preemptive_tts": PREEMPTIVE_GENERATION,
            },
            interruption={
                "enabled": True,
                # Production mode disables adaptive interruption by default.
                # Barge-in is the headline behaviour here, so ask for it
                # explicitly rather than inheriting whatever the mode implies.
                "mode": "adaptive",
                "min_duration": INTERRUPTION_MIN_DURATION,
                "min_words": INTERRUPTION_MIN_WORDS,
                # If we cut in on a false positive, resume instead of dropping
                # the thought -- a real caller doesn't abandon their sentence.
                "resume_false_interruption": True,
            },
        ),
    )


class Patient(Agent):
    def __init__(self, scenario: Scenario, hung_up: asyncio.Event) -> None:
        super().__init__(instructions=scenario.instructions())
        self._hung_up = hung_up

    @function_tool
    async def end_call(self, ctx: RunContext, reason: str) -> None:
        """Hang up the phone.

        Only call this after you have already said your goodbye out loud -- the
        line drops the moment you use this.

        Args:
            reason: short note on why the call is over, e.g. "appointment booked"
                or "they would not give me the refill".
        """
        logger.info("patient is hanging up: %s", reason)
        await ctx.wait_for_playout()  # let the goodbye finish playing
        await get_job_context().delete_room()
        # Signal the end here rather than relying on the session's close event:
        # on call 01 that event took longer than the remaining cap to arrive, so
        # the run looked like a timeout when the patient had in fact finished.
        self._hung_up.set()


def prewarm(proc: JobProcess) -> None:
    """Load the VAD once per worker process, not once per call."""
    proc.userdata["vad"] = silero.VAD.load(min_silence_duration=VAD_MIN_SILENCE)


# num_idle_processes: the production default is 14, which on a laptop spawns 14
# job runners at once and blocked the event loop for over a second at a time --
# enough to miss the room-ready handshake and panic the Rust FFI layer. We place
# one call at a time, so one spare runner is all we need.
#
# port=0: the worker serves health checks over HTTP, defaulting to a fixed 8081.
# If a previous worker is still holding that port -- e.g. it was orphaned when its
# parent was killed -- the next worker dies at startup with WinError 10048 before
# it can register, which cost us a scenario. 0 lets the OS pick a free port, so
# back-to-back runs and stale workers can never collide.
#
# load_threshold: in production mode the worker marks itself unavailable above
# 0.7 CPU load, which is right for autoscaling a fleet and wrong for a laptop
# placing one call at a time. On a machine running anything else it flaps in and
# out of capacity mid-call -- 17 times in one observed run -- and the call never
# completes. Dev mode uses inf for exactly this reason; we want the same.
server = AgentServer(
    setup_fnc=prewarm,
    num_idle_processes=1,
    port=0,
    load_threshold=float("inf"),
)


@server.rtc_session(agent_name=AGENT_NAME)
async def patient_session(ctx: JobContext) -> None:
    job = json.loads(ctx.job.metadata or "{}")
    scenario = load_scenario(job["scenario"])
    call_index = int(job["call_index"])

    # Validate everything the call needs up front. Failing here costs nothing;
    # failing after session.start() has already connected to the room doesn't.
    number = assert_allowed_number(TARGET_NUMBER)
    trunk_id = require_env("LIVEKIT_SIP_TRUNK_ID")

    logger.info(
        "call %02d | scenario=%s | persona=%s | stt=%s llm=%s tts=%s turn=%s",
        call_index,
        scenario.id,
        scenario.name,
        STT_PROVIDER,
        LLM_CACHING,
    LLM_PROVIDER,
        TTS_PROVIDER,
        TURN_DETECTOR,
    VAD_MIN_SILENCE,
    )

    session = build_session(scenario, vad=ctx.proc.userdata["vad"])

    # Save the OGG and transcript before the job's temp directory is cleaned up.
    # A call costs real money, so this must not depend on a clean shutdown: we
    # save explicitly once the call ends, and register the same function as a
    # shutdown callback in case we never get that far (cancelled job, crash).
    saved = False

    async def _save() -> None:
        nonlocal saved
        if saved:
            return
        try:
            save_artifacts(
                ctx.make_session_report(session), scenario, call_index, latency=latency
            )
            saved = True
        except Exception:
            logger.exception("failed to save artifacts for call %02d", call_index)

    ctx.add_shutdown_callback(_save)

    # Set either by the patient's end_call tool or by the session closing
    # (which is how a call ends when the *far end* hangs up first).
    latency = LatencyLog()
    session.on("metrics_collected", lambda ev: latency.record(ev.metrics))

    call_over = asyncio.Event()
    session.on("close", lambda _ev: call_over.set())

    # Connect explicitly before starting the session. session.start() would
    # schedule this as a background task, which races the room-ready handshake;
    # doing it here means the room is up before anything depends on it.
    await ctx.connect()

    await session.start(
        agent=Patient(scenario, call_over),
        room=ctx.room,
        # audio=True is what makes the framework write the local stereo OGG.
        record=RecordingOptions(audio=True, transcript=True, traces=True, logs=False),
    )

    # ---- the only place this project dials anything -----------------------
    # `number` came from assert_allowed_number() at the top of this function;
    # nothing else in the codebase reaches add_sip_participant.
    logger.info("dialing %s via trunk %s", number, trunk_id)
    await ctx.add_sip_participant(
        call_to=number,
        trunk_id=trunk_id,
        participant_identity="office",
        participant_name="Office receptionist",
    )
    logger.info("call answered")

    # The far end speaks first on an outbound call, so we normally just listen.
    # Scenarios that test barge-in start talking over the greeting instead.
    if scenario.speak_first_after:

        async def _barge_in() -> None:
            await asyncio.sleep(scenario.speak_first_after)
            if not call_over.is_set():
                logger.info("barging in over the greeting")
                session.generate_reply()

        asyncio.create_task(_barge_in())

    try:
        await asyncio.wait_for(call_over.wait(), timeout=MAX_CALL_SECONDS)
        logger.info("call %02d ended normally", call_index)
    except asyncio.TimeoutError:
        logger.warning(
            "call %02d hit the %ds cap -- hanging up", call_index, MAX_CALL_SECONDS
        )
        await ctx.delete_room()

    # aclose() flushes and finalises the OGG encoder, so the file on disk is
    # complete before we copy it. Saving here rather than waiting for job
    # shutdown means a paid call survives the worker being killed afterwards.
    await session.aclose()
    await _save()
    logger.info("call %02d artifacts written", call_index)


if __name__ == "__main__":
    agents.cli.run_app(server)
