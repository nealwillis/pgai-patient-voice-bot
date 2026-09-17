"""Curate the recordings a reviewer sees first.

    python curate.py --dry-run     # show the plan, touch nothing
    python curate.py               # apply it

37 calls were recorded across tuning iterations, accent passes and latency
experiments. A reviewer should not have to guess which one to play. This keeps
the best call per scenario in recordings/, and moves everything else to
recordings/extra/ where it is still available as evidence.

Selection: one call per scenario, preferring the most recent (most tuned) clean
call, scored on whether it is a full conversation, opens like a person, and
answers promptly. call-01 is excluded by rule -- it is the pre-tuning baseline
and is deliberately kept in extra/ as the "before" case.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import dataclass

import numpy as np

from src.config import PROJECT_ROOT, RECORDINGS_DIR, TRANSCRIPTS_DIR
from src.pacing import analyze_audio

EXTRA_DIR = RECORDINGS_DIR / "extra"
TARGET_KEEP = 12
BASELINE_CALL = 1  # pre-tuning; never featured


@dataclass
class Call:
    index: int
    scenario: str
    audio: object
    transcript: object
    duration: float = 0.0
    turns: int = 0
    opener_words: int = 0
    latency: float = 0.0

    @property
    def stem(self) -> str:
        return f"call-{self.index:02d}-{self.scenario}"

    def score(self) -> float:
        """Higher is better. Rewards a full, natural, prompt conversation."""
        if self.index == BASELINE_CALL:
            return -1e9
        s = 0.0
        # A real conversation, comfortably inside the 1-3 minute window.
        s += 40 if 90 <= self.duration <= 180 else 0
        s += min(self.turns, 30)                      # richer exchange
        s -= max(0, self.opener_words - 12) * 2.5     # paragraph openers are the tell
        s -= self.latency * 8                         # dead air before we reply
        s += self.index * 0.4                         # later = more tuned
        return s


def collect() -> list[Call]:
    calls: list[Call] = []
    for t in sorted(TRANSCRIPTS_DIR.glob("call-*-transcript.txt")):
        m = re.match(r"call-(\d+)-(.+)-transcript\.txt$", t.name)
        if not m:
            continue
        idx, scen = int(m.group(1)), m.group(2)
        audio = RECORDINGS_DIR / f"call-{idx:02d}-{scen}.ogg"
        if not audio.exists():
            audio = EXTRA_DIR / f"call-{idx:02d}-{scen}.ogg"
        c = Call(index=idx, scenario=scen, audio=audio, transcript=t)

        lines = [ln for ln in t.read_text(encoding="utf-8").splitlines() if ln.startswith("[")]
        c.turns = len(lines)
        for ln in lines:
            if "PATIENT:" in ln:
                c.opener_words = len(ln.split("PATIENT:", 1)[1].split())
                break
        if audio.exists():
            try:
                p = analyze_audio(audio)
                c.duration = p.duration
                c.latency = float(np.median(p.our_latency)) if p.our_latency else 0.0
            except Exception:  # noqa: BLE001
                pass
        calls.append(c)
    return calls


def choose(calls: list[Call]) -> tuple[list[Call], list[Call]]:
    """Best call per scenario, then the strongest scenarios up to TARGET_KEEP."""
    best: dict[str, Call] = {}
    for c in calls:
        if c.duration < 60 or c.turns < 6:
            continue  # not a usable conversation
        cur = best.get(c.scenario)
        if cur is None or c.score() > cur.score():
            best[c.scenario] = c
    ranked = sorted(best.values(), key=lambda c: c.score(), reverse=True)
    keep = ranked[:TARGET_KEEP]
    keep_idx = {c.index for c in keep}
    extra = [c for c in calls if c.index not in keep_idx]
    return sorted(keep, key=lambda c: c.index), sorted(extra, key=lambda c: c.index)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    calls = collect()
    keep, extra = choose(calls)

    print(f"{len(calls)} calls on disk -> keeping {len(keep)}, moving {len(extra)} to extra/\n")
    print(f"  {'#':>3}  {'scenario':18} {'dur':>6} {'opener':>7} {'latency':>8} {'turns':>6}")
    print("  " + "-" * 60)
    for c in keep:
        print(f"  {c.index:3}  {c.scenario:18} {c.duration:5.0f}s {c.opener_words:6}w "
              f"{c.latency:7.2f}s {c.turns:6}")
    print(f"\n  to extra/: {', '.join(c.stem for c in extra)}\n")

    if args.dry_run:
        print("dry run -- nothing moved")
        return 0

    EXTRA_DIR.mkdir(parents=True, exist_ok=True)
    moved = 0
    for c in extra:
        src = RECORDINGS_DIR / f"{c.stem}.ogg"
        if src.exists():
            shutil.move(str(src), str(EXTRA_DIR / f"{c.stem}.ogg"))
            moved += 1
    # And pull anything previously demoted back, if it is now a keeper.
    restored = 0
    for c in keep:
        src = EXTRA_DIR / f"{c.stem}.ogg"
        if src.exists():
            shutil.move(str(src), str(RECORDINGS_DIR / f"{c.stem}.ogg"))
            restored += 1
    print(f"moved {moved} to extra/, restored {restored} to recordings/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
