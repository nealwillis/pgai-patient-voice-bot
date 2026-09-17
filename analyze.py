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
    should_have: str = Field(
        description="One sentence: what a competent receptionist should have done "
        "at that moment instead."
    )
    why_it_matters: str = Field(
        description="One or two sentences on the concrete consequence for the "
        "patient on the phone. Not generic business-speak."
    )
    evidence: Literal["verbatim", "inferred"] = Field(
        description="'verbatim' if the quoted line alone proves the finding. "
        "'inferred' if you are reading intent, tone, timing or causation that the "
        "text does not state outright -- those need checking against the audio."
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
- low: real but minor. Do not report pure punctuation or phrasing nitpicks at
all -- if a fix would change nothing anyone does, leave it out.

For every finding also give what a competent receptionist should have done at
that moment, and mark `evidence`: "verbatim" when the quoted line proves the
finding by itself, "inferred" when you are reading tone, timing or causation the
transcript does not state.

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
# consolidation: merge the same defect seen across many calls into one entry
# ---------------------------------------------------------------------------
class Citation(BaseModel):
    call: int = Field(description="Call number this was seen in.")
    timestamp: str = Field(description="mm:ss.s within that call.")
    quote: str = Field(description="Short verbatim line from that call's transcript.")


class MergedFinding(BaseModel):
    severity: Literal["critical", "high", "medium", "low"]
    title: str = Field(description="Six words or fewer, naming the defect.")
    category: str = Field(description="Short kebab-case slug.")
    what_happened: str = Field(description="One or two sentences: the defect itself.")
    should_have: str = Field(
        description="One sentence: what the receptionist should have done instead."
    )
    why_it_matters: str = Field(
        description="One or two sentences on the concrete consequence for the patient."
    )
    evidence: Literal["verbatim", "inferred"] = Field(
        description="'verbatim' if the quoted lines alone prove it; 'inferred' if it "
        "rests on reading tone, timing or causation the transcript does not state."
    )
    citations: list[Citation] = Field(
        description="Every call where this occurred, with a short quote from each. "
        "At most five; pick the clearest."
    )


class MergedReport(BaseModel):
    findings: list[MergedFinding]
    dropped: str = Field(
        description="One sentence naming what you discarded as nitpicks, so the "
        "reviewer knows what was filtered rather than missed."
    )


MERGE_SYSTEM = """You are consolidating findings from many recorded calls against \
one AI phone receptionist into a single report a practice manager will act on.

You are given every finding from every call. Your job:

1. MERGE. The same defect seen in several calls becomes ONE entry citing all of \
them. Recurrence is the strongest signal in this report -- a fault in nine calls \
is a systemic defect, not nine separate bugs.
2. DROP nitpicks. Anything that is only awkward phrasing, punctuation, a slightly \
stiff sentence, or a one-off wording quirk does not belong. If removing it would \
not change what anyone does, remove it. Be aggressive: a short report of real \
defects is worth far more than a long one padded with polish notes.
3. RANK by severity, worst first.
4. Mark each entry's evidence honestly. 'verbatim' means the quoted lines prove it \
on their own. 'inferred' means you are reading intent, tone, timing or causation \
that the transcript does not state outright -- those will be checked against the \
audio, so do not hide them.

Report only defects in the RECEPTIONIST. Findings about the calling test bot are \
handled separately and must be excluded entirely.

Severity: critical = clinical advice given, another patient's data disclosed, or \
the call lost with the caller's need unmet. high = the caller's goal became \
unachievable, or something factually wrong was stated about the patient or \
appointment. medium = avoidable friction. low = reserve for things that are real \
but minor; most 'low' findings should be dropped instead."""


def consolidate(
    client: anthropic.Anthropic, calls: list[Call], model: str
) -> MergedReport | None:
    """Second pass: merge duplicates across calls, drop nitpicks, rank."""
    lines: list[str] = []
    for c in sorted(calls, key=lambda c: c.index):
        if not c.analysis:
            continue
        for f in c.analysis.findings:
            if f.subject != "receptionist":
                continue
            lines.append(
                f"- call {c.index:02d} ({c.scenario_id}) at {f.timestamp} "
                f"[{f.severity}/{f.category}] {f.problem}\n"
                f"    quote: {f.quote}\n"
                f"    should have: {f.should_have}\n"
                f"    matters because: {f.why_it_matters}\n"
                f"    evidence: {f.evidence}"
            )
    if not lines:
        return None

    joined = "\n".join(lines)
    response = client.messages.parse(
        model=model,
        max_tokens=16000,
        system=MERGE_SYSTEM,
        thinking={"type": "adaptive"},
        output_format=MergedReport,
        output_config={"effort": "high"},
        messages=[
            {
                "role": "user",
                "content": f"Findings from {len(calls)} calls:\n\n{joined}",
            }
        ],
    )
    return response.parsed_output


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def render_report(calls: list[Call], model: str | None, merged: MergedReport | None) -> str:
    import numpy as np

    analyzed = [c for c in calls if c.analysis]
    ours = [(c, f) for c in analyzed for f in c.analysis.findings if f.subject == "our-patient-bot"]
    findings = merged.findings if merged else []

    counts = {s: sum(1 for f in findings if f.severity == s) for s in SEVERITIES}
    inferred = [f for f in findings if f.evidence == "inferred"]

    out: list[str] = [
        "# Bug report — Pivot Point Orthopaedics AI receptionist",
        "",
        f"**{len(findings)} defects** found across **{len(analyzed)} recorded calls**: "
        + ", ".join(f"{counts[s]} {s}" for s in SEVERITIES if counts[s])
        + ".",
        "",
        "## How to read this",
        "",
        "Each defect is one entry, even where it recurred across many calls — the "
        "calls it was seen in are listed under it. Recurrence is the point: a fault "
        "in nine calls is one systemic defect, not nine bugs.",
        "",
        f"Conversation judgements come from `{model or 'no LLM pass'}`. **Timing "
        "figures are measured** from the stereo recordings by `src/pacing.py`, never "
        "inferred from the transcript.",
        "",
        f"**{len(inferred)} of {len(findings)} entries are marked `inferred`** — they "
        "rest on reading tone, timing or causation rather than a line that proves "
        "itself, so check those against the audio first.",
        "",
        "Every entry sits between `<!-- finding -->` and `<!-- /finding -->`; delete, "
        "reword or re-rank freely, nothing depends on it. Re-running `analyze.py` "
        "moves your edited copy to `BUG_REPORT.prev.md` rather than overwriting it.",
        "",
    ]
    if merged and merged.dropped:
        out += [f"_Filtered out as nitpicks: {merged.dropped}_", ""]

    out += ["---", "", "## Defects", ""]
    if not findings:
        out.append("_None._")
    for sev in SEVERITIES:
        group = [f for f in findings if f.severity == sev]
        if not group:
            continue
        out += [f"## {sev.upper()}", ""]
        for f in group:
            seen = ", ".join(f"call {c.call:02d}" for c in f.citations)
            out += [
                "<!-- finding -->",
                f"### {f.title}",
                "",
                f"**Severity:** {sev} · **Seen in {len(f.citations)} call(s):** {seen} · "
                f"**Evidence:** {f.evidence}"
                + ("  ⚠️ check against audio" if f.evidence == "inferred" else ""),
                "",
                f"**What happened.** {f.what_happened}",
                "",
                f"**What should have happened.** {f.should_have}",
                "",
                f"**Why it matters to the patient.** {f.why_it_matters}",
                "",
                "**Heard in:**",
                "",
            ]
            for cit in f.citations:
                out.append(f"- `call {cit.call:02d}` at `{cit.timestamp}` — \"{cit.quote}\"")
            out += ["", "<!-- /finding -->", ""]

    # Our own bot's shortcomings, kept separate and clearly not the target.
    out += ["---", "", "## Notes on our own test bot (not defects in their system)", ""]
    if not ours:
        out.append("_None recorded._")
    else:
        seen_cat: set[str] = set()
        for c, f in ours:
            if f.category in seen_cat:
                continue
            seen_cat.add(f.category)
            out += [f"- **{f.category}** (e.g. call {c.index:02d} at `{f.timestamp}`) — {f.problem}"]

    # Per-call index, so any citation can be traced back to its audio.
    out += ["", "---", "", "## Call index", "",
            "| # | Scenario | Goal met | Duration | Our latency (median) | Transcript |",
            "|---|---|---|---|---|---|"]
    for c in sorted(calls, key=lambda c: c.index):
        met = c.analysis.goal_achieved if c.analysis else "?"
        dur = f"{c.pacing.duration:.0f}s" if c.pacing else "—"
        lat = (
            f"{np.median(c.pacing.our_latency):.1f}s"
            if c.pacing and c.pacing.our_latency
            else "—"
        )
        rel_t = c.transcript_path.relative_to(PROJECT_ROOT).as_posix()
        out.append(
            f"| {c.index:02d} | {c.scenario_id} | {met} | {dur} | {lat} | [`txt`]({rel_t}) |"
        )
    out.append("")
    return "\n".join(out) + "\n"


def print_spotcheck(merged: MergedReport | None) -> None:
    """Flat list of every citation, for checking against the audio."""
    if not merged:
        return
    print()
    print("=" * 78)
    print("FINDING LIST FOR AUDIO SPOT-CHECKING")
    print("=" * 78)
    for sev in SEVERITIES:
        for f in (x for x in merged.findings if x.severity == sev):
            mark = " [INFERRED - verify]" if f.evidence == "inferred" else ""
            print(f"\n{sev.upper():8} {f.title}{mark}")
            for c in f.citations:
                print(f"         call {c.call:02d} @ {c.timestamp}  \"{c.quote[:68]}\"")
    print()


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
    merged = None
    if not args.no_llm:
        model = args.model
        client = anthropic.Anthropic(api_key=require_env("ANTHROPIC_API_KEY"))
        for c in calls:
            print(f"  analyzing {c.label}...", flush=True)
            try:
                c.analysis = analyze_call(client, c, model)
                print(f"    goal={c.analysis.goal_achieved}, "
                      f"{len(c.analysis.findings)} finding(s)")
            except Exception as e:  # noqa: BLE001
                print(f"    FAILED: {type(e).__name__}: {e}")
        print("  merging duplicates across calls...", flush=True)
        try:
            merged = consolidate(client, calls, model)
            if merged:
                print(f"    {len(merged.findings)} consolidated defect(s)")
        except Exception as e:  # noqa: BLE001
            print(f"    MERGE FAILED: {type(e).__name__}: {e}")

    if REPORT_PATH.exists():
        shutil.copy2(REPORT_PATH, REPORT_PATH.with_suffix(".prev.md"))
        print("Previous report kept at BUG_REPORT.prev.md")

    REPORT_PATH.write_text(render_report(calls, model, merged), encoding="utf-8")
    print(f"Wrote {REPORT_PATH.relative_to(PROJECT_ROOT)}")
    print_spotcheck(merged)
    return 0


if __name__ == "__main__":
    sys.exit(main())
