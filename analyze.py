"""Read every call transcript, flag problems with an LLM, write BUG_REPORT.md.

    python analyze.py                  # analyze everything in transcripts/
    python analyze.py --call 03        # just one call
    python analyze.py --no-llm         # pacing metrics only, no API spend

Each finding is emitted as a self-contained block delimited by HTML comments, so
the report is easy to hand-edit: delete a block, reword it, or change a severity
without disturbing anything else. An existing BUG_REPORT.md is moved aside to
BUG_REPORT.prev.md rather than overwritten, so hand edits are never lost silently.

The LLM judges the conversation; it is not asked to judge timing. Response
latency, think time and talk-over are measured from the stereo audio instead
(src/pacing.py) and handed to the model as facts, because inferring pacing from
transcript timestamps produced a wrong answer early in this project.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from src.config import (
    PROJECT_ROOT,
    RECORDINGS_DIR,
    TRANSCRIPTS_DIR,
    env,
    require_env,
)
from src.pacing import Pacing, analyze_audio
from src.scenarios import Scenario, all_scenarios

REPORT_PATH = PROJECT_ROOT / "BUG_REPORT.md"
SEVERITIES = ["critical", "high", "medium", "low"]


# ---------------------------------------------------------------------------
# what we ask the model for
# ---------------------------------------------------------------------------
class Finding(BaseModel):
    subject: Literal["receptionist", "our-patient-bot"] = Field(
        description="Whose behaviour is at fault. The receptionist is the system "
        "under test; our-patient-bot findings are for our own iteration."
    )
    severity: Literal["critical", "high", "medium", "low"]
    category: str = Field(
        description="Short kebab-case slug, e.g. 'cannot-book-new-patient', "
        "'talks-over-caller', 'ignores-question', 'wrong-data-readback'."
    )
    timestamp: str = Field(description="mm:ss.s from the transcript, or '-' if it spans the call.")
    quote: str = Field(description="The exact line from the transcript that shows it.")
    problem: str = Field(description="One sentence: what went wrong.")
    why_it_matters: str = Field(
        description="One or two sentences on the real consequence for a patient "
        "or the practice. Be concrete, not generic."
    )


class CallAnalysis(BaseModel):
    summary: str = Field(description="Two sentences on what happened in this call.")
    goal_achieved: Literal["yes", "partial", "no"]
    goal_note: str = Field(description="One sentence on why the goal was or wasn't met.")
    findings: list[Finding]


SYSTEM = """You are reviewing recordings of an AI phone receptionist for an \
orthopaedics practice. A test harness called it while role-playing patients, and \
you are reading the transcript of one call.

Your job is to find real defects in the RECEPTIONIST's behaviour. Judge it the way \
a practice manager would: did the caller get what they phoned for, was anything \
said that was wrong or unsafe, was the caller treated like a person.

Report a finding only when you can point at a specific line that demonstrates it. \
Quote that line exactly. Do not invent problems to fill space -- a call with two \
real findings should return two findings, and a clean call should return none.

Severity:
- critical: gave clinical advice, disclosed another patient's data, or lost the \
call entirely with the caller's need unmet.
- high: the caller's goal became unachievable, or the system stated something \
factually wrong about the patient or the appointment.
- medium: avoidable friction -- redundant questions, ignored questions, talking \
over the caller, confusing phrasing.
- low: polish. Awkward wording, minor inconsistency.

You may also report findings about OUR patient bot (subject 'our-patient-bot') \
when it behaved unlike a real caller -- reciting paragraphs, missing an obvious \
cue, breaking character. Keep those separate from receptionist findings.

Timing facts are measured from the audio and given to you. Use those numbers; do \
not try to infer timing from the transcript timestamps, which mark when a turn was \
captured rather than when it began."""


@dataclass
class Call:
    index: int
    scenario_id: str
    transcript_path: Path
    audio_path: Path | None
    text: str
    scenario: Scenario | None
    pacing: Pacing | None = None
    analysis: CallAnalysis | None = None

    @property
    def label(self) -> str:
        return f"call {self.index:02d} ({self.scenario_id})"


def discover_calls(only: int | None) -> list[Call]:
    scenarios = {s.id: s for s in all_scenarios()}
    calls: list[Call] = []
    for path in sorted(TRANSCRIPTS_DIR.glob("call-*-transcript.txt")):
        m = re.match(r"call-(\d+)-(.+)-transcript\.txt$", path.name)
        if not m:
            continue
        index, scenario_id = int(m.group(1)), m.group(2)
        if only is not None and index != only:
            continue
        audio = RECORDINGS_DIR / f"call-{index:02d}-{scenario_id}.ogg"
        calls.append(
            Call(
                index=index,
                scenario_id=scenario_id,
                transcript_path=path,
                audio_path=audio if audio.exists() else None,
                text=path.read_text(encoding="utf-8"),
                scenario=scenarios.get(scenario_id),
            )
        )
    return calls


def analyze_call(client: anthropic.Anthropic, call: Call, model: str) -> CallAnalysis:
    criteria = ""
    if call.scenario:
        bullets = "\n".join(f"- {c}" for c in call.scenario.success_criteria)
        criteria = (
            f"\nWhat this call was testing: {call.scenario.label}\n"
            f"The caller's goal: {call.scenario.goal.strip()}\n"
            f"What would count as success:\n{bullets}\n"
        )

    pacing = f"\nMeasured from the audio:\n{call.pacing.summary()}\n" if call.pacing else ""

    # `output_format` takes the Pydantic class; the SDK converts it to a schema
    # and merges it into output_config.format itself. Passing the class inside
    # output_config directly is not serializable.
    response = client.messages.parse(
        model=model,
        max_tokens=8000,
        system=SYSTEM,
        thinking={"type": "adaptive"},
        output_format=CallAnalysis,
        output_config={"effort": "high"},
        messages=[
            {
                "role": "user",
                "content": f"{criteria}{pacing}\nTranscript:\n\n{call.text}",
            }
        ],
    )
    parsed = response.parsed_output
    if parsed is None:
        raise RuntimeError(f"model returned no parsable output (stop: {response.stop_reason})")
    return parsed


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def render_report(calls: list[Call], model: str | None) -> str:
    analyzed = [c for c in calls if c.analysis]
    findings = [(c, f) for c in analyzed for f in c.analysis.findings]
    theirs = [(c, f) for c, f in findings if f.subject == "receptionist"]
    ours = [(c, f) for c, f in findings if f.subject == "our-patient-bot"]

    out: list[str] = [
        "# Bug report — Pivot Point Orthopaedics AI receptionist",
        "",
        f"{len(calls)} call(s) reviewed, {len(theirs)} finding(s) against the "
        f"receptionist and {len(ours)} against our own test bot.",
        "",
        "## How to read and edit this",
        "",
        "Generated by `python analyze.py`. Conversation judgements come from "
        f"`{model or 'no LLM pass'}`; **timing figures are measured** from the stereo "
        "recordings by `src/pacing.py`, not inferred from the transcript.",
        "",
        "Every finding is a self-contained block between `<!-- finding -->` and "
        "`<!-- /finding -->`. Delete a block, reword it, or change its severity "
        "freely — nothing else depends on it. Re-running `analyze.py` moves your "
        "edited copy to `BUG_REPORT.prev.md` rather than overwriting it.",
        "",
        "Severity: **critical** = clinical advice, data disclosure, or call lost "
        "with the need unmet · **high** = goal became unachievable or wrong facts "
        "stated · **medium** = avoidable friction · **low** = polish.",
        "",
        "## Calls",
        "",
        "| # | Scenario | Goal met | Findings | Duration | Our latency (median) | Their think time |",
        "|---|---|---|---|---|---|---|",
    ]

    for c in sorted(calls, key=lambda c: c.index):
        n = len([f for f in (c.analysis.findings if c.analysis else []) if f.subject == "receptionist"])
        met = c.analysis.goal_achieved if c.analysis else "?"
        if c.pacing and c.pacing.our_latency:
            import numpy as np

            dur = f"{c.pacing.duration:.0f}s"
            ours_med = f"{np.median(c.pacing.our_latency):.1f}s"
            theirs_med = (
                f"{np.median(c.pacing.their_latency):.1f}s" if c.pacing.their_latency else "—"
            )
        else:
            dur = f"{c.pacing.duration:.0f}s" if c.pacing else "—"
            ours_med = theirs_med = "—"
        out.append(
            f"| {c.index:02d} | {c.scenario_id} | {met} | {n} | {dur} | {ours_med} | {theirs_med} |"
        )

    for title, group in (
        ("Findings — their receptionist", theirs),
        ("Findings — our test bot (for our own iteration)", ours),
    ):
        out += ["", f"## {title}", ""]
        if not group:
            out.append("_None._")
            continue
        by_sev: dict[str, list] = defaultdict(list)
        for c, f in group:
            by_sev[f.severity].append((c, f))
        for sev in SEVERITIES:
            items = by_sev.get(sev, [])
            if not items:
                continue
            out += [f"### {sev.title()} ({len(items)})", ""]
            # Cluster by category so a recurring problem reads as one theme.
            by_cat: dict[str, list] = defaultdict(list)
            for c, f in items:
                by_cat[f.category].append((c, f))
            for cat, entries in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
                seen_in = ", ".join(f"call {c.index:02d}" for c, _ in entries)
                out += [
                    f"#### `{cat}` — seen in {len(entries)} call(s): {seen_in}",
                    "",
                ]
                for c, f in entries:
                    out += [
                        "<!-- finding -->",
                        f"**{c.label}** at `{f.timestamp}` — {f.problem}",
                        "",
                        f"> {f.quote}",
                        "",
                        f"{f.why_it_matters}",
                        "",
                        "<!-- /finding -->",
                        "",
                    ]

    out += ["", "## Per-call detail", ""]
    for c in sorted(calls, key=lambda c: c.index):
        out += [f"### Call {c.index:02d} — {c.scenario_id}", ""]
        if c.analysis:
            out += [
                c.analysis.summary,
                "",
                f"**Goal {c.analysis.goal_achieved}.** {c.analysis.goal_note}",
                "",
            ]
        if c.pacing:
            out += ["```", c.pacing.summary(), "```", ""]
        rel_t = c.transcript_path.relative_to(PROJECT_ROOT).as_posix()
        out.append(f"Transcript: [`{rel_t}`]({rel_t})")
        if c.audio_path:
            rel_a = c.audio_path.relative_to(PROJECT_ROOT).as_posix()
            out.append(f" · Audio: [`{rel_a}`]({rel_a})")
        out.append("")

    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--call", type=int, help="only analyze this call number")
    ap.add_argument("--no-llm", action="store_true", help="pacing only, no API spend")
    ap.add_argument("--model", default=env("ANALYZER_MODEL", "claude-opus-5"))
    args = ap.parse_args()

    calls = discover_calls(args.call)
    if not calls:
        print(f"No transcripts found in {TRANSCRIPTS_DIR}")
        return 1
    print(f"Found {len(calls)} transcript(s).")

    for c in calls:
        if c.audio_path:
            try:
                c.pacing = analyze_audio(c.audio_path)
            except Exception as e:  # noqa: BLE001 -- a bad file shouldn't stop the report
                print(f"  {c.label}: could not measure audio ({type(e).__name__}: {e})")
        else:
            print(f"  {c.label}: no audio found, skipping pacing")

    model = None
    if not args.no_llm:
        model = args.model
        client = anthropic.Anthropic(api_key=require_env("ANTHROPIC_API_KEY"))
        for c in calls:
            print(f"  analyzing {c.label} with {model}...", flush=True)
            try:
                c.analysis = analyze_call(client, c, model)
                n = len(c.analysis.findings)
                print(f"    goal={c.analysis.goal_achieved}, {n} finding(s)")
            except Exception as e:  # noqa: BLE001
                print(f"    FAILED: {type(e).__name__}: {e}")

    if REPORT_PATH.exists():
        backup = REPORT_PATH.with_suffix(".prev.md")
        shutil.copy2(REPORT_PATH, backup)
        print(f"Previous report kept at {backup.name}")

    REPORT_PATH.write_text(render_report(calls, model), encoding="utf-8")
    print(f"Wrote {REPORT_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
