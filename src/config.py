"""Environment config, and the one guard that keeps us from dialing the wrong number."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

RECORDINGS_DIR = PROJECT_ROOT / "recordings"
TRANSCRIPTS_DIR = PROJECT_ROOT / "transcripts"
SCENARIOS_DIR = PROJECT_ROOT / "scenarios"

# ---------------------------------------------------------------------------
# The Pretty Good AI test line. This is the ONLY number this project may dial.
# Hardcoded deliberately: not read from the environment, not overridable by a
# CLI flag, not part of any scenario file.
# ---------------------------------------------------------------------------
TARGET_NUMBER = "+18054398008"

# Hard ceiling on a single call, enforced by the agent regardless of the LLM.
MAX_CALL_SECONDS = 180


class DialGuardError(RuntimeError):
    """Raised when something tries to dial a number other than TARGET_NUMBER."""


def assert_allowed_number(number: str | None) -> str:
    """Return the canonical target number, or raise if `number` is anything else.

    This is called on the single code path that can place a call. It compares
    digits only — so "+18054398008", "(805) 439-8008" and "805-439-8008" are all
    accepted as the same destination — and returns the canonical E.164 form, so
    callers dial the exact value the guard approved rather than their own input.
    Anything that is not this number, in any format, raises.
    """
    digits = "".join(c for c in (number or "") if c.isdigit())
    if len(digits) == 10:  # bare NANP national form
        digits = "1" + digits
    if digits != TARGET_NUMBER.lstrip("+"):
        raise DialGuardError(
            f"refusing to dial {number!r}: this project may only call {TARGET_NUMBER}"
        )
    return TARGET_NUMBER


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set — see .env.example")
    return value


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip() or default


# --- provider selection (swappable during Phase 2 tuning) ------------------
STT_PROVIDER = env("STT_PROVIDER", "deepgram")  # "deepgram" (direct) | "livekit"
LLM_PROVIDER = env("LLM_PROVIDER", "anthropic")
TTS_PROVIDER = env("TTS_PROVIDER", "cartesia")
TURN_DETECTOR = env("TURN_DETECTOR", "inference")  # "inference" | "local"

ANTHROPIC_MODEL = env("ANTHROPIC_MODEL", "claude-haiku-4-5")
# Prompt caching. The system prompt is ~5.3k characters and identical on every
# turn of a call, which is exactly the shape caching exists for. Measured LLM
# time-to-first-token was 1.59s without it -- ~70% of the whole response budget.
LLM_CACHING = env("LLM_CACHING", "1") not in {"0", "false", "no"}
OPENAI_MODEL = env("OPENAI_MODEL", "gpt-4o-mini")
CARTESIA_MODEL = env("CARTESIA_MODEL", "sonic-3")
# Deepgram model id. Direct: "nova-3". Via LiveKit: "deepgram/nova-3".
# "nova-2-phonecall" is telephony-tuned and worth an A/B in Phase 2.
DEEPGRAM_MODEL = env("DEEPGRAM_MODEL", "nova-3")

# --- endpointing / interruption knobs (the Phase 2 dials) -----------------
# Deliberately surfaced as env vars so we can tune pacing between calls
# without editing code. Defaults are the starting point, not the answer.
ENDPOINTING_MIN_DELAY = float(env("ENDPOINTING_MIN_DELAY", "0.25"))
# Endpointing mode: "fixed" waits a constant delay; "dynamic" keeps a moving
# average of the far end's pacing and adapts.
ENDPOINTING_MODE = env("ENDPOINTING_MODE", "fixed")
# The ceiling when the turn detector is unsure the far end has finished. At 3.0s
# this was the source of the intermittent long waits heard on call 01 -- most
# replies were fast, but an uncertain endpoint stalled the full three seconds.
ENDPOINTING_MAX_DELAY = float(env("ENDPOINTING_MAX_DELAY", "2.0"))
# How much silence Silero needs before it reports end-of-speech. This is the real
# floor on every reply: ENDPOINTING_MIN_DELAY is back-dated to when they last
# spoke, so it cannot bite below this number. Silero's own default is 0.55s and
# we shipped that untouched. The turn detector's hard minimum is 0.25s.
VAD_MIN_SILENCE = float(env("VAD_MIN_SILENCE", "0.30"))
INTERRUPTION_MIN_DURATION = float(env("INTERRUPTION_MIN_DURATION", "0.4"))
INTERRUPTION_MIN_WORDS = int(env("INTERRUPTION_MIN_WORDS", "2"))
# Start generating (and speaking) before the turn is confirmed. Cuts perceived
# latency at the cost of occasionally discarding a generation.
PREEMPTIVE_GENERATION = env("PREEMPTIVE_GENERATION", "1") not in {"0", "false", "no"}
