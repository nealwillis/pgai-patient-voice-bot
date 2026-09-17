"""Audit the call deliverables against the submission requirements.

    python audit_deliverables.py

Checks every call has both a transcript and an audio file, that the audio is a
real OGG/Opus of 1-3 minutes, and that the call was an actual two-way
conversation rather than a failed or aborted connection. Exits non-zero if
fewer than MIN_CLEAN_CALLS are clean.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

import av

from src.config import PROJECT_ROOT, RECORDINGS_DIR, TRANSCRIPTS_DIR

MIN_CLEAN_CALLS = 10
MIN_SECONDS = 60
MAX_SECONDS = 185  # 3 min plus the couple of seconds teardown adds
MIN_TURNS = 6


@dataclass
class Row:
    index: int
    scenario: str
    transcript: str
    audio: str
    fmt: str = "—"
    seconds: float | None = None
    turns: int = 0
    receptionist_turns: int = 0
    patient_turns: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.problems


def audit() -> list[Row]:
    rows: list[Row] = []
    for t in sorted(TRANSCRIPTS_DIR.glob("call-*-transcript.txt")):
        m = re.match(r"call-(\d+)-(.+)-transcript\.txt$", t.name)
        if not m:
            continue
        index, scenario = int(m.group(1)), m.group(2)
        audio = RECORDINGS_DIR / f"call-{index:02d}-{scenario}.ogg"
        row = Row(
            index=index,
            scenario=scenario,
            transcript=t.name,
            audio=audio.name if audio.exists() else "MISSING",
        )

        text = t.read_text(encoding="utf-8")
        lines = [ln for ln in text.splitlines() if ln.startswith("[")]
        row.turns = len(lines)
        row.receptionist_turns = sum("RECEPTIONIST:" in ln for ln in lines)
        row.patient_turns = sum("PATIENT:" in ln for ln in lines)

        if "no conversation captured" in text:
            row.problems.append("no conversation captured")
        if row.turns < MIN_TURNS:
            row.problems.append(f"only {row.turns} turns")
        if row.patient_turns == 0:
            row.problems.append("our patient never spoke")
        if row.receptionist_turns == 0:
            row.problems.append("far end never spoke - call not answered")

        if not audio.exists():
            row.problems.append("audio missing")
        else:
            try:
                with av.open(str(audio)) as c:
                    s = c.streams.audio[0]
                    row.fmt = (
                        f"{c.format.name}/{s.codec_context.name} "
                        f"{s.codec_context.layout.name} {s.codec_context.sample_rate}Hz"
                    )
                    row.seconds = float(s.duration * s.time_base)
                if s.codec_context.name not in {"opus", "mp3"}:
                    row.problems.append(f"codec {s.codec_context.name} is not OGG/Opus or MP3")
                if row.seconds is not None:
                    if row.seconds < MIN_SECONDS:
                        row.problems.append(f"under 1 min ({row.seconds:.0f}s)")
                    elif row.seconds > MAX_SECONDS:
                        row.problems.append(f"over 3 min ({row.seconds:.0f}s)")
            except Exception as e:  # noqa: BLE001
                row.problems.append(f"audio unreadable ({type(e).__name__})")

        rows.append(row)
    return rows


def main() -> int:
    rows = audit()
    if not rows:
        print("No calls found.")
        return 1

    print(f"{'#':>3}  {'scenario':18} {'dur':>6} {'turns':>5} {'them/us':>8}  {'format':34} status")
    print("-" * 104)
    for r in rows:
        dur = f"{r.seconds:.0f}s" if r.seconds is not None else "—"
        status = "clean" if r.clean else "; ".join(r.problems)
        print(
            f"{r.index:3}  {r.scenario:18} {dur:>6} {r.turns:5} "
            f"{f'{r.receptionist_turns}/{r.patient_turns}':>8}  {r.fmt:34} {status}"
        )

    clean = [r for r in rows if r.clean]
    scen = {}
    for r in clean:
        scen[r.scenario] = scen.get(r.scenario, 0) + 1
    dupes = {k: v for k, v in scen.items() if v > 1}

    print()
    print(f"{len(rows)} call(s) on disk, {len(clean)} clean.")
    print(f"Distinct scenarios among clean calls: {len(scen)}")
    if dupes:
        print("Repeated scenarios (tuning iterations): " + ", ".join(f"{k}x{v}" for k, v in dupes.items()))
    if bad := [r for r in rows if not r.clean]:
        print(f"\nNOT clean ({len(bad)}):")
        for r in bad:
            print(f"  call {r.index:02d} {r.scenario}: {'; '.join(r.problems)}")

    print()
    if len(clean) < MIN_CLEAN_CALLS:
        print(f"FAIL: need {MIN_CLEAN_CALLS} clean calls, have {len(clean)}.")
        return 1
    print(f"PASS: {len(clean)} clean calls (requirement: {MIN_CLEAN_CALLS}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
