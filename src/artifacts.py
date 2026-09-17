"""Write the two deliverables for a finished call: the OGG audio and the transcript.

The agent framework already records a stereo OGG/Opus file for us (channel 0 =
the far end, channel 1 = our patient) into a per-job temp directory. We copy it
somewhere permanent before the job's temp dir is cleaned up, and render the
chat history alongside it with timestamps relative to that same audio.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import shutil
from pathlib import Path

from .config import RECORDINGS_DIR, TARGET_NUMBER, TRANSCRIPTS_DIR

logger = logging.getLogger("patient.artifacts")

# Our agent's "user" is the office's receptionist; our own bot is the assistant.
SPEAKER_LABELS = {"user": "RECEPTIONIST", "assistant": "PATIENT"}


def next_call_index() -> int:
    """Lowest unused call number, so re-runs never overwrite a paid call."""
    used = {
        int(m.group(1))
        for d, pat in ((RECORDINGS_DIR, "call-*.ogg"), (TRANSCRIPTS_DIR, "call-*.txt"))
        for f in d.glob(pat)
        if (m := re.match(r"call-(\d+)", f.name))
    }
    n = 1
    while n in used:
        n += 1
    return n


def _mmss(seconds: float) -> str:
    # Round to tenths *before* splitting, or a value like 59.95 renders as "00:60.0".
    tenths = max(0, round(seconds * 10))
    minutes, rem = divmod(tenths, 600)
    return f"{minutes:02d}:{rem / 10:04.1f}"


def _text_of(message) -> str:
    parts = [c for c in message.content if isinstance(c, str)]
    return " ".join(p.strip() for p in parts if p.strip())


def render_transcript(report, scenario, index: int, audio_name: str | None) -> str:
    """Timestamped, speaker-labeled transcript. Timestamps are audio-relative."""
    t0 = report.audio_recording_started_at or report.started_at or report.timestamp
    started = dt.datetime.fromtimestamp(report.started_at or report.timestamp)

    head = [
        f"# Call {index:02d} — {scenario.id} ({scenario.label})",
        f"# Patient persona : {scenario.name}",
        f"# Number dialed   : {TARGET_NUMBER}",
        f"# Started         : {started:%Y-%m-%d %H:%M:%S} (local)",
        f"# Duration        : {report.duration:.1f}s" if report.duration else "# Duration        : unknown",
        f"# Audio           : {audio_name or 'MISSING'}",
        f"# Room            : {report.room}",
        "#",
        "# RECEPTIONIST = the office's AI receptionist (audio channel 0)",
        "# PATIENT      = our bot                      (audio channel 1)",
        "#",
        "# Timestamps are mm:ss.s from the start of the audio, and mark when the",
        "# turn was captured, not when it began: a RECEPTIONIST stamp is when their",
        "# speech finished transcribing, a PATIENT stamp is when our reply started.",
        "# So the interval between a PATIENT line and the next RECEPTIONIST line is",
        "# their think time plus their speaking time. Use the audio to separate the",
        "# two, and to hear overlaps.",
        "",
    ]

    lines: list[str] = []
    for item in report.chat_history.items:
        if getattr(item, "type", None) != "message":
            continue
        if item.role not in SPEAKER_LABELS:
            continue  # skip the system prompt
        text = _text_of(item)
        if not text:
            continue
        stamp = _mmss((item.created_at or t0) - t0)
        flag = "  [interrupted]" if getattr(item, "interrupted", False) else ""
        lines.append(f"[{stamp}] {SPEAKER_LABELS[item.role]}: {text}{flag}")

    if not lines:
        lines.append("(no conversation captured — the call may not have connected)")

    return "\n".join(head + lines) + "\n"


def save_artifacts(report, scenario, index: int, latency=None) -> dict[str, Path | None]:
    """Copy the audio and write the transcript. Returns what actually landed."""
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

    stem = f"call-{index:02d}-{scenario.id}"
    audio_dest: Path | None = None

    src = report.audio_recording_path
    if src and Path(src).exists():
        audio_dest = RECORDINGS_DIR / f"{stem}.ogg"
        shutil.copy2(src, audio_dest)
        logger.info("saved audio -> %s (%.1f KB)", audio_dest, audio_dest.stat().st_size / 1024)
    else:
        logger.error("no audio recording found (expected at %s)", src)

    transcript_dest = TRANSCRIPTS_DIR / f"{stem}-transcript.txt"
    rel_audio = f"recordings/{audio_dest.name}" if audio_dest else None
    body = render_transcript(report, scenario, index, rel_audio)
    if latency is not None:
        measured = "\n".join("# " + ln for ln in latency.summary().splitlines())
        body += (
            "\n# Where the pause before each of our replies went\n"
            "# (measured by the framework per turn, not inferred from the text)\n"
            "#\n" + measured + "\n"
        )
    transcript_dest.write_text(body, encoding="utf-8")
    logger.info("saved transcript -> %s", transcript_dest)

    return {"audio": audio_dest, "transcript": transcript_dest}
