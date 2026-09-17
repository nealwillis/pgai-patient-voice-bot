"""Measure conversational pacing from a call recording.

The recordings are stereo with the two sides on separate channels -- channel 0 is
the office's receptionist, channel 1 is our patient -- so who was speaking when is
a measurement, not a guess. That lets us state their think time and any talk-over
as numbers, instead of asking an LLM to infer them from a transcript.

Energy-based speech detection per channel: frame the audio, compare each frame
against that channel's own noise floor, then smooth. Deliberately simple -- we
only need to know who held the floor, not to transcribe anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import av
import numpy as np

FRAME_MS = 20
SPEECH_START_FRAMES = 3   # 60ms of energy to call it speech
SPEECH_END_FRAMES = 10    # 200ms of quiet to call it over
NOISE_MULTIPLIER = 4.0    # how far above the channel's floor counts as speech

THEM, US = 0, 1
LABELS = {THEM: "receptionist", US: "patient"}


@dataclass
class Segment:
    channel: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class Pacing:
    duration: float
    segments: list[Segment]
    # Quiet between their turn ending and ours starting: our response latency.
    our_latency: list[float] = field(default_factory=list)
    # Quiet between our turn ending and theirs starting: their think time.
    their_latency: list[float] = field(default_factory=list)
    # (start, duration, who_started_talking_over) for simultaneous speech.
    overlaps: list[tuple[float, float, str]] = field(default_factory=list)
    speaking_time: dict[str, float] = field(default_factory=dict)

    def summary(self) -> str:
        """A compact, quotable block for the bug report and the LLM prompt."""

        def stats(xs: list[float], name: str) -> str:
            if not xs:
                return f"  {name}: no measurable turns"
            arr = np.array(xs)
            return (
                f"  {name}: n={len(xs)}  median={np.median(arr):.1f}s  "
                f"mean={arr.mean():.1f}s  max={arr.max():.1f}s"
            )

        lines = [
            f"  call duration: {self.duration:.1f}s",
            f"  speaking time: receptionist {self.speaking_time.get('receptionist', 0):.1f}s, "
            f"patient {self.speaking_time.get('patient', 0):.1f}s",
            stats(self.their_latency, "their think time (silence before they reply)"),
            stats(self.our_latency, "our response latency (silence before we reply)"),
        ]
        if self.overlaps:
            longest = max(self.overlaps, key=lambda o: o[1])
            lines.append(
                f"  talk-over: {len(self.overlaps)} overlap(s), longest {longest[1]:.1f}s "
                f"at {longest[0]:.1f}s (started by the {longest[2]})"
            )
        else:
            lines.append("  talk-over: none detected")
        return "\n".join(lines)


def _speech_mask(samples: np.ndarray, frame_len: int) -> np.ndarray:
    """Per-frame speech/no-speech for one channel.

    Scale-agnostic: Opus decodes to float in [-1, 1] but WAV-ish sources come
    back as int16, so thresholds are expressed as a fraction of full scale
    rather than in absolute sample units.
    """
    n_frames = len(samples) // frame_len
    if n_frames == 0:
        return np.zeros(0, dtype=bool)
    framed = samples[: n_frames * frame_len].reshape(n_frames, frame_len).astype(np.float64)

    full_scale = 32768.0 if np.abs(framed).max() > 1.5 else 1.0
    rms = np.sqrt(((framed / full_scale) ** 2).mean(axis=1))

    # The channel's own noise floor, plus an absolute gate so a silent channel
    # doesn't read its own rounding noise as speech (-46 dBFS).
    floor = float(np.percentile(rms, 10))
    return rms > max(floor * NOISE_MULTIPLIER, 0.005)


def _segments(mask: np.ndarray, channel: int) -> list[Segment]:
    """Collapse a frame mask into speech segments, with hysteresis."""
    out: list[Segment] = []
    start: int | None = None
    quiet = 0
    for i, loud in enumerate(mask):
        if loud:
            quiet = 0
            if start is None:
                start = i
        elif start is not None:
            quiet += 1
            if quiet >= SPEECH_END_FRAMES:
                end = i - quiet + 1
                if end - start >= SPEECH_START_FRAMES:
                    out.append(Segment(channel, start * FRAME_MS / 1000, end * FRAME_MS / 1000))
                start, quiet = None, 0
    if start is not None and len(mask) - start >= SPEECH_START_FRAMES:
        out.append(Segment(channel, start * FRAME_MS / 1000, len(mask) * FRAME_MS / 1000))
    return out


def analyze_audio(path: Path) -> Pacing:
    """Decode a stereo call recording and measure its turn-taking."""
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        rate = stream.codec_context.sample_rate
        chunks = [
            f.to_ndarray().reshape(-1, 2) if f.to_ndarray().ndim == 1 else f.to_ndarray()
            for f in container.decode(audio=0)
        ]

    if not chunks:
        return Pacing(duration=0.0, segments=[])

    # PyAV hands back either (channels, samples) or interleaved; normalise to
    # (samples, channels).
    data = np.concatenate(
        [c.T if c.shape[0] == 2 and c.ndim == 2 else c for c in chunks], axis=0
    )
    if data.ndim == 1:  # mono fallback -- treat it as the far end only
        data = np.column_stack([data, np.zeros_like(data)])

    frame_len = int(rate * FRAME_MS / 1000)
    duration = len(data) / rate

    segments: list[Segment] = []
    speaking: dict[str, float] = {}
    for ch in (THEM, US):
        segs = _segments(_speech_mask(data[:, ch], frame_len), ch)
        segments.extend(segs)
        speaking[LABELS[ch]] = sum(s.duration for s in segs)
    segments.sort(key=lambda s: s.start)

    pacing = Pacing(duration=duration, segments=segments, speaking_time=speaking)

    # Latency: a handover is one side's segment ending, then the other starting.
    for a, b in zip(segments, segments[1:]):
        if a.channel == b.channel:
            continue
        gap = b.start - a.end
        if gap < 0:
            continue  # overlap, handled below
        (pacing.our_latency if b.channel == US else pacing.their_latency).append(gap)

    # Overlap: any pair of opposite-channel segments that intersect.
    for a in (s for s in segments if s.channel == THEM):
        for b in (s for s in segments if s.channel == US):
            lo, hi = max(a.start, b.start), min(a.end, b.end)
            if hi > lo:
                # Whoever started second is the one talking over.
                intruder = LABELS[US] if b.start > a.start else LABELS[THEM]
                pacing.overlaps.append((lo, hi - lo, intruder))
    pacing.overlaps.sort()

    return pacing
