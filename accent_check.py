"""Did their receptionist hear the caller's name correctly, by accent?

    python accent_check.py

Every call follows the same shape: the caller gives a name, and the receptionist
reads it back to confirm. That read-back is a clean, comparable probe of their
speech recognition, and it happens once per call regardless of scenario -- so it
works as a natural experiment across the accents.

This reads the transcripts only. It costs nothing and makes no calls.
"""

from __future__ import annotations

import difflib
import re
import sys

from src.config import TRANSCRIPTS_DIR
from src.scenarios import all_scenarios

# Calls before this index were recorded before per-persona accents existed, so
# they all used one general-american voice. Their scenario's accent label does
# not apply to them and must not be read as an accent result.
ACCENT_PASS_FROM = 19

# The receptionist's confirmation phrasings, seen across the recorded calls.
READBACK = re.compile(
    r"(?:your name as|name as|speaking with|I have your name(?: as)?|this is)\s+([A-Z][\w'\-]*(?:\s+[A-Z][\w'\-]*){0,2})"
)


def accent_of(scenario) -> str:
    """Read the accent from the YAML comment we annotate each scenario with."""
    if not scenario.path:
        return "?"
    m = re.search(r"#\s*Accent under test:\s*([a-z\-]+)", scenario.path.read_text(encoding="utf-8"))
    if m:
        return m.group(1)
    m = re.search(r"#\s*Language under test:\s*(.+)", scenario.path.read_text(encoding="utf-8"))
    return m.group(1).strip().rstrip(".") if m else "-"


def main() -> int:
    scenarios = {s.id: s for s in all_scenarios()}
    rows = []

    for path in sorted(TRANSCRIPTS_DIR.glob("call-*-transcript.txt")):
        m = re.match(r"call-(\d+)-(.+)-transcript\.txt$", path.name)
        if not m:
            continue
        index, sid = int(m.group(1)), m.group(2)
        scenario = scenarios.get(sid)
        if not scenario:
            continue

        true_name = scenario.name

        heard: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if "RECEPTIONIST:" not in line:
                continue
            for cand in READBACK.findall(line):
                cand = cand.strip()
                # Only keep read-backs that are plausibly an attempt at this name.
                if difflib.SequenceMatcher(None, cand.lower(), true_name.lower()).ratio() > 0.45:
                    heard.append(cand)

        best = max(
            heard,
            key=lambda h: difflib.SequenceMatcher(None, h.lower(), true_name.lower()).ratio(),
            default="",
        )
        ratio = (
            difflib.SequenceMatcher(None, best.lower(), true_name.lower()).ratio() if best else 0.0
        )
        # Every spoken part must match. Checking only the surname passed
        # "Tony" for "Toni" and even "Sarah" for "Carol", which are exactly the
        # mishearings this is supposed to catch.
        def norm(s: str) -> list[str]:
            return [w for w in re.sub(r"[^\w\s]", "", s.lower()).split() if w]

        exact = bool(best) and norm(best) == norm(true_name)
        accent = accent_of(scenario) if index >= ACCENT_PASS_FROM else "(pre-accent run)"
        rows.append((index, sid, accent, true_name, best, ratio, exact))

    if not rows:
        print("No transcripts found.")
        return 1

    print(f"{'#':>3}  {'accent':22} {'we said':18} {'they heard':22} {'match':>6}  ok")
    print("-" * 88)
    for idx, sid, accent, true_name, best, ratio, exact in rows:
        shown = best or "(never read the name back)"
        mark = "yes" if exact else ("NO" if best else "--")
        print(f"{idx:3}  {accent:22} {true_name:18} {shown:22} {ratio:5.0%}  {mark}")

    got = [r for r in rows if r[4]]
    wrong = [r for r in got if not r[6]]
    accented = [r for r in rows if r[0] >= ACCENT_PASS_FROM]
    print()
    print(f"{len(rows)} call(s); the name was read back in {len(got)}.")
    if accented:
        a_got = [r for r in accented if r[4]]
        a_bad = [r for r in a_got if not r[6]]
        print(f"Of the {len(accented)} accent-pass call(s): {len(a_got)} read the name back, "
              f"{len(a_bad)} got it wrong.")
    else:
        print("No accent-pass calls yet -- every call above used one general-american voice.")
    if wrong:
        print(f"Misheard in {len(wrong)}:")
        for idx, sid, accent, true_name, best, ratio, _ in wrong:
            print(f"  call {idx:02d}  {accent:20} {true_name!r} -> {best!r}")
    else:
        print("No mishearings detected.")
    print()
    print("Caveats: one accent per scenario, so accent is confounded with what the")
    print("caller was asking for; and a name can be misheard by our own STT rather than")
    print("theirs. Treat this as a pointer to calls worth listening to, not as a")
    print("measurement of accuracy per accent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
