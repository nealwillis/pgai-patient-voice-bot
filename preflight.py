"""Check everything that can be checked without spending money.

    python preflight.py

Run this before run_calls.py. It verifies .env is complete, the scenarios parse,
the provider clients construct, and the dial guard is still the only way to
reach the phone network.
"""

from __future__ import annotations

import pathlib
import sys

from src import config
from src.scenarios import all_scenarios

# Absolute, so preflight works from any working directory. Relative paths here
# used to fail with a misleading "FileNotFoundError: run_calls.py".
CHECKED_SOURCES = [
    *sorted((config.PROJECT_ROOT / "src").glob("*.py")),
    config.PROJECT_ROOT / "run_calls.py",
    config.PROJECT_ROOT / "setup_trunk.py",
    config.PROJECT_ROOT / "analyze.py",
]

REQUIRED_ENV = [
    "LIVEKIT_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
    "LIVEKIT_SIP_TRUNK_ID",
]
PROVIDER_ENV = {
    "deepgram": ["DEEPGRAM_API_KEY"],
    "livekit": [],  # uses the LIVEKIT_* credentials already checked above
    "anthropic": ["ANTHROPIC_API_KEY"],
    "openai": ["OPENAI_API_KEY"],
    "cartesia": ["CARTESIA_API_KEY"],
    "elevenlabs": ["ELEVEN_API_KEY", "ELEVENLABS_VOICE_ID"],
}
# Only needed by setup_trunk.py, not by a call once the trunk exists.
TRUNK_ENV = [
    "TWILIO_TERMINATION_URI",
    "TWILIO_SIP_USERNAME",
    "TWILIO_SIP_PASSWORD",
    "OUTBOUND_CALLER_ID",
]


def _lines_containing(needle: str) -> list[str]:
    return [
        f"{p.relative_to(config.PROJECT_ROOT).as_posix()}:{i}"
        for p in CHECKED_SOURCES
        for i, ln in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if needle in ln and not ln.strip().startswith("#")
    ]


def main() -> int:
    problems: list[str] = []
    notes: list[str] = []

    def check(ok: bool, label: str, detail: str = "") -> None:
        print(f"  {'ok  ' if ok else 'FAIL'} {label}{f' -- {detail}' if detail else ''}")
        if not ok:
            problems.append(f"{label}{f': {detail}' if detail else ''}")

    print("Dial safety")
    dial_sites = _lines_containing("add_sip_participant(")
    check(len(dial_sites) == 1, "exactly one dial call site", ", ".join(dial_sites))
    literals = {s.split(":")[0] for s in _lines_containing(config.TARGET_NUMBER.lstrip("+"))}
    check(
        literals <= {"src/config.py"},
        "target number only hardcoded in src/config.py",
        ", ".join(sorted(literals - {"src/config.py"})),
    )
    guard_ok = True
    for bad in ["+18054398009", "+15551234567", "911", "", None]:
        try:
            config.assert_allowed_number(bad)
            guard_ok = False
        except config.DialGuardError:
            pass
    check(guard_ok, "guard rejects every other number")
    check(
        config.assert_allowed_number("805-439-8008") == config.TARGET_NUMBER,
        "guard normalises to E.164",
    )

    print("\nScenarios")
    try:
        scenarios = all_scenarios()
        check(len(scenarios) >= 10, f"{len(scenarios)} scenarios parse (need 10+)")
        ids = [s.id for s in scenarios]
        check(len(ids) == len(set(ids)), "scenario ids are unique")
        check(
            all(s.instructions().strip() for s in scenarios), "every scenario builds a prompt"
        )
    except Exception as e:
        check(False, "scenarios load", str(e))

    print("\nEnvironment")
    for name in REQUIRED_ENV:
        check(bool(config.env(name)), name)
    for name in PROVIDER_ENV.get(config.STT_PROVIDER, []):
        check(bool(config.env(name)), f"{name} (STT_PROVIDER={config.STT_PROVIDER})")
    for name in PROVIDER_ENV.get(config.LLM_PROVIDER, []):
        check(bool(config.env(name)), f"{name} (LLM_PROVIDER={config.LLM_PROVIDER})")
    for name in PROVIDER_ENV.get(config.TTS_PROVIDER, []):
        check(bool(config.env(name)), f"{name} (TTS_PROVIDER={config.TTS_PROVIDER})")
    missing_trunk = [n for n in TRUNK_ENV if not config.env(n)]
    if missing_trunk:
        notes.append(
            "setup_trunk.py will need: " + ", ".join(missing_trunk)
        )

    print("\nProviders")
    import src.patient as patient

    for label, build in [
        (f"STT ({config.STT_PROVIDER})", patient._build_stt),
        (f"LLM ({config.LLM_PROVIDER})", patient._build_llm),
        (f"TTS ({config.TTS_PROVIDER})", patient._build_tts),
    ]:
        try:
            build()
            check(True, label)
        except Exception as e:
            check(False, label, f"{type(e).__name__}: {e}")

    if config.TURN_DETECTOR == "local":
        notes.append(
            "TURN_DETECTOR=local can only be constructed inside a job, so neither "
            "it nor the full session is verified here -- both are exercised on the "
            "first real call."
        )
    else:
        try:
            patient._build_turn_detection()
            check(True, "turn detector (inference)")
        except Exception as e:
            check(False, "turn detector (inference)", str(e))

        # Build the real session with the real turn-handling config, so a bad
        # option key fails here for free instead of wasting a paid call.
        try:
            session = patient.build_session(all_scenarios()[0])
            o = session.options
            check(
                o.endpointing["max_delay"] == config.ENDPOINTING_MAX_DELAY,
                "session accepts the turn-handling config",
            )
            print(f"       endpointing  {o.endpointing}")
            print(f"       interruption {o.interruption}")
            print(f"       preemptive   {o.preemptive_generation}")
        except Exception as e:
            check(False, "session construction", f"{type(e).__name__}: {e}")

    print("\nSettings")
    print(f"  endpointing  {config.ENDPOINTING_MIN_DELAY}s - {config.ENDPOINTING_MAX_DELAY}s")
    print(f"  interruption {config.INTERRUPTION_MIN_DURATION}s / {config.INTERRUPTION_MIN_WORDS} words")
    print(f"  call cap     {config.MAX_CALL_SECONDS}s")
    print(f"  dials        {config.TARGET_NUMBER} only")

    for note in notes:
        print(f"\nnote: {note}")

    print()
    if problems:
        print(f"PREFLIGHT FAILED ({len(problems)} problem(s)) -- do not place calls yet:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("PREFLIGHT PASSED -- safe to run run_calls.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
