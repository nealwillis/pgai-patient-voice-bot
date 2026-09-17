"""Collect the per-turn latency budget the framework already measures.

`pacing.py` tells us the total gap before our patient speaks. This tells us where
that gap went. The agents framework emits a metrics event per pipeline stage; we
were throwing all of it away, which meant every tuning decision was an argument
rather than arithmetic.

The four numbers that matter, per turn:

    end_of_utterance_delay  how long we waited before deciding they had finished
    transcription_delay     how late the final transcript arrived
    ttft                    LLM time to first token
    ttfb                    TTS time to first audio byte
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class LatencyLog:
    """Accumulates per-stage metrics for one call."""

    eou_delay: list[float] = field(default_factory=list)
    transcription_delay: list[float] = field(default_factory=list)
    ttft: list[float] = field(default_factory=list)
    ttfb: list[float] = field(default_factory=list)
    llm_cancelled: int = 0
    tts_cancelled: int = 0

    def record(self, m) -> None:
        """Feed one AgentMetrics event."""
        kind = getattr(m, "type", "")
        if kind == "eou_metrics":
            self._add(self.eou_delay, getattr(m, "end_of_utterance_delay", None))
            self._add(self.transcription_delay, getattr(m, "transcription_delay", None))
        elif kind == "llm_metrics":
            if getattr(m, "cancelled", False):
                # A cancelled generation is preemptive work that got thrown away.
                self.llm_cancelled += 1
            else:
                self._add(self.ttft, getattr(m, "ttft", None))
        elif kind == "tts_metrics":
            if getattr(m, "cancelled", False):
                self.tts_cancelled += 1
            else:
                self._add(self.ttfb, getattr(m, "ttfb", None))

    @staticmethod
    def _add(target: list[float], value) -> None:
        # Negative or absurd values show up when a stage is skipped; drop them
        # rather than let them poison a median.
        if isinstance(value, (int, float)) and 0 <= float(value) < 30:
            target.append(float(value))

    def summary(self) -> str:
        rows = [
            ("waiting for them to finish", self.eou_delay),
            ("final transcript arriving", self.transcription_delay),
            ("LLM first token", self.ttft),
            ("TTS first audio byte", self.ttfb),
        ]
        out = ["  stage                        n   median      mean       max"]
        total = 0.0
        for label, xs in rows:
            if not xs:
                out.append(f"  {label:26}   -        -         -         -")
                continue
            a = np.array(xs)
            total += float(np.median(a))
            out.append(
                f"  {label:26} {len(a):3}   {np.median(a):6.2f}s   "
                f"{a.mean():6.2f}s   {a.max():6.2f}s"
            )
        out.append(f"  {'sum of medians':26}       {total:6.2f}s")
        if self.llm_cancelled or self.tts_cancelled:
            out.append(
                f"  discarded preemptive work: {self.llm_cancelled} LLM, "
                f"{self.tts_cancelled} TTS"
            )
        return "\n".join(out)
