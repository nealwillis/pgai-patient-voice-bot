# Changelog

What changed, and why. Newest first.

---

## Phase 2, iteration 2 — calls 02 and 03

### Both prompt fixes landed

Call 02's opener was still three sentences, so the base prompt gained an explicit
rule: *the very first turn is one short headline sentence, nothing else.* Call 03
opened with **"Hi — yeah, I need to get my knee looked at."** The name-correction
guidance also worked first time: asked "Am I speaking with Marcus?", the patient
replied **"No, this is Maya. I think you've got my husband's number on file"** and
their system accepted it and moved on.

Turn length is fixed generally — "Yeah, okay.", "W-E-B-B.", "Yep, that's right.",
"No, that's not right." These are people, not paragraphs.

### Measuring pacing from audio instead of guessing (`src/pacing.py`)

Added because inferring pacing from transcript timestamps produced a wrong answer
once already. It detects speech per channel (channel 0 = them, channel 1 = us)
against each channel's own noise floor, then derives response latency, think time,
speaking time and talk-over as measurements. Two bugs while building it: the
threshold assumed int16 samples but Opus decodes to float, so nothing registered
as speech at all; and a module-level `_frame_len` global got threaded through
properly instead.

This is what turned "their latency is 8–23s" (wrong) into "their think time is
~2.5s and they occupy 77.5s of a 182.5s call" (measured), and what showed the
tuning cut our own latency from 2.8s to 1.7s median.

### The record-lookup dead-end — confirmed as their bug

Call 02, with an unrecognised name, spent ninety seconds on identity
verification (name → DOB → spell it → phone → confirm → confirm again) and then
transferred to a "patient support team" that answered *"You've reached the Pretty
Good AI test line. Goodbye"* and hung up. No appointment.

Call 03 was a deliberate probe: same scenario, but the patient discloses being a
new patient in her own words. It made no difference —

> "Since you're a new patient and I couldn't find your record, I'm not able to
> book directly right now."

So **a new patient cannot book through their receptionist at all.** This is a
confirmed product bug rather than our patient failing to disclose, and it shapes
the rest of the run: `reschedule`, `cancel`, `refill` and the two follow-up
scenarios all presuppose a record we cannot create.

Call 03 also surfaced a bug the transcript alone would never have suggested:
their agent read **our Twilio caller ID** back as the patient's own number on
file, unprompted, then interrupted her correction with "Is that correct?", then
fractured the read-back across a four-second gap. And on call 02, both detected
talk-overs were *them* speaking over *us*.

### Runner resilience

The full 14-call run aborted on its first call when the worker process died
silently, milliseconds after the call was answered. It did not reproduce on
retry, and I could not attribute it, because I had filtered the worker's output
through `grep` at the source and discarded its dying words — the same pipe also
masked the real exit code. Lesson applied twice: don't filter a log you might
need, and don't pipe a command whose exit status matters.

Rather than chase an unreproducible fault, the runner now tolerates it: a dead
worker is restarted and the run continues, with one retry per scenario, instead
of abandoning the remaining calls.

---

## Phase 2, iteration 1 — after listening to call 01

Feedback on call 01: female voice against male persona names, sounded too
polished, and it waited too long to respond *at times*.

### "At times" was the useful word

I first read call 01's transcript as showing 0.1–0.5s replies and concluded the
lag was intermittent. **That reading was wrong** — transcript timestamps record
when a reply was *generated*, not when audio reached the line, so they omit TTS
time-to-first-byte entirely. Measuring the audio per channel afterwards gave the
truth: **2.8s median, 5.2s worst case**. The complaint was accurate and my
inference from the transcript was not; this is why `src/pacing.py` now measures
pacing from audio instead of inferring it from text.

The intermittent character still pointed at the right knob. When the turn
detector isn't confident the far end has finished, it waits out the full
`max_delay` ceiling, which was **3.0s** — and 5.2s worst case is that ceiling
plus TTS startup.

| Knob | Was | Now | Why |
|---|---|---|---|
| `ENDPOINTING_MAX_DELAY` | 3.0s | **2.0s** | Caps the stall on an uncertain endpoint — the intermittent wait |
| `ENDPOINTING_MIN_DELAY` | 0.35s | **0.25s** | Shaves the floor off every reply |
| `preemptive_generation` | off | **on** (+ `preemptive_tts`) | Starts generating and speaking before the turn is confirmed; held off in Phase 1 to keep the baseline simple, and this is exactly what it's for |

Measured result on call 02: response latency **2.8s → 1.7s median**, worst case
**5.2s → 2.3s**.

Deliberately **not** changed: `endpointing.mode` stays `fixed`. There is a
`dynamic` mode that keeps an exponential moving average of the far end's pacing
and would plausibly suit a consistently slow receptionist, but six things changed
this round already and a seventh would make attribution impossible. It's the next
lever if waits persist.

### "Too polished"

The tell on call 01 was the opener: *"Yeah, hi. I've got a dull ache in my lower
left jaw that's been going on for about a week, and I'd like to get an
appointment to have someone look at it."* Twenty-five words, perfectly
constructed. Nobody opens a phone call with a paragraph.

Two causes, both fixed:

1. **`max_tokens` was 120** — roughly ninety words of headroom. Now **60**. A
   ceiling is cruder than a prompt but it cannot be argued with.
2. **The scenario's `reason` field reads like prose**, and the model was
   paraphrasing it rather than converting it to speech. The prompt now states
   explicitly that these sections are the situation written down, *not* lines to
   read, and `base_prompt.md` gained paired no/yes examples for openers and for
   answering questions — "April twelfth, eighty-four", not "My date of birth is
   April twelfth, nineteen eighty-four." Examples move a model; adjectives don't.
   Also added occasional self-correction ("I've had this — well, it's my knee").

### Voice and names

All six male personas renamed to match the female voice (Marcus→Maya,
Harold→Harriet, Tony→Toni, Vernon→Vera, Edgar→Esther, Craig→Carol). Scenarios
also gained an optional `voice:` field that overrides the env voice per persona,
so supplying a second voice id later buys variety without touching code.

### Preflight

Extended to construct the real `AgentSession` with the actual turn-handling
config, so a bad option key fails for free instead of wasting a call.

---

## Phase 1 — call 01, and what it changed

First real call: `new-appointment`, 182.5s, appointment booked at 9:30am. Audio
and transcript in `recordings/` and `transcripts/`.

### Bugs fixed (three attempts to get one usable call)

**Attempt 1 — never dialed.** `FFI Panic: timed out waiting for
ReadyForRoomEventRequest`. `AgentServer` defaults to `num_idle_processes=14` in
production mode; spawning 14 job runners on a laptop blocked the event loop for
over a second at a time (`event loop blocked for 1213ms`), long enough to miss
the room-ready handshake. Set to 1, and now call `ctx.connect()` explicitly
instead of letting `session.start()` race it as a background task. The same log
revealed `adaptive interruption is disabled by default in production mode`,
which would have quietly weakened barge-in — now requested explicitly via
`interruption.mode`.

**Attempt 2 — dialed, talked for 113s, artifacts lost.** A `UnicodeEncodeError`
writing a worker log line to a cp1252 Windows console killed the runner, whose
`__exit__` then terminated the worker before its shutdown callback could save
anything. We paid for a call and got no recording. Three changes: force UTF-8 on
both ends of the pipe, make `drain_output` incapable of raising, and give the
worker a grace period on teardown. Most importantly, **the agent no longer
depends on a clean shutdown to save** — it calls `session.aclose()` (which
flushes the OGG encoder) and writes artifacts as soon as the call ends, with the
shutdown callback as an idempotent fallback.

**Attempt 3 — succeeded.** One further fix after the fact: the patient hung up
at 171s but the session's `close` event arrived after the 180s cap, so the run
logged a misleading timeout. `end_call` now signals completion directly.

### What call 01 revealed about the target

- **It's an orthopaedics practice** — "Pivot Point Orthopaedics, part of Pretty
  Good AI" — not a dental office. Six scenarios referencing teeth, gums, a
  hygienist and a cleaning were rewritten around knees, shoulders, wrists, hips
  and anti-inflammatories.
- **Turn-to-turn intervals of 8–23s**, but that conflates their think time with
  their speaking time. Per-channel measurement of the audio (added later, see
  `src/pacing.py`) shows the real split: their think time is only ~2.4s median.
  The gaps were mostly them *talking* — 77.5s of a 182.5s call, against our
  patient's 26.3s. Their verbosity, not their latency, is the finding.
- **They key off caller ID.** Before our patient gave a name, they asked "Am I
  speaking with Marcus?" — they had stored Marcus Webb from the lost attempt 2.
  Since all calls must come from one number, every persona will now be greeted as
  Marcus. Rather than fight it, the base prompt now tells patients to correct the
  name naturally ("I think you've got my husband's number on file"), which turns
  the collision into a test of whether they can override a caller-ID assumption.
- Provider name rendered three ways in one call: "doctor Zigbigmie", then
  "doctor Zigniew Likoski" twice. Needs the audio to attribute to their TTS or
  our STT.

### Transcript format

Header originally implied timestamps marked when a line was spoken. They mark
turn *capture* — a RECEPTIONIST stamp is when their speech finished
transcribing. Reworded, because a reader would otherwise mis-measure every gap.

---

## Phase 1 — scaffold (before any call)

### Stack decisions

**Telephony: Twilio Elastic SIP Trunk, not LiveKit Phone Numbers.**
LiveKit now sells its own numbers, which would have removed Twilio entirely, but
[their numbers are inbound-only](https://docs.livekit.io/telephony/start/phone-numbers/)
today. Outbound still needs a third-party trunk. Also confirmed that a Twilio
*trial* account cannot SIP-dial an unverified number (error 32100), and the PGAI
line can't be verified because verification requires entering a code on the
answering end — so the account has to be funded. Reusing an existing unused
number instead of buying one; a number just has to be voice-capable and assigned
to the trunk.

**Recording: the framework's own local OGG, not LiveKit Egress.**
Originally planned room-composite egress to Cloudflare R2, because
[LiveKit Cloud egress can't write locally](https://docs.livekit.io/home/egress/outputs/)
("a request that resolves to no storage fails"). While reading the installed
1.8.2 source I found `AgentSession` already writes a **stereo OGG/Opus** file
itself via `RecorderIO` → `session_directory/audio.ogg`, with channel 0 = the far
end and channel 1 = our patient, time-aligned. We copy it out in a shutdown
callback.

Three reasons this beat egress:
1. No object-storage account, no bucket credentials, no download step.
2. Channel separation. A composite mix flattens both speakers together; separate
   channels make it possible to *hear* exactly who talked over whom, which is the
   whole point of the barge-in scenarios.
3. It's the format the deliverable asks for (OGG) with no transcode.

This is a deviation from the requested stack. Egress remains the right answer if
recordings needed to land in shared storage rather than on one laptop.

**LLM: Claude Haiku 4.5 (`claude-haiku-4-5`).**
~$0.02/call, so cost isn't the deciding factor against gpt-4o-mini. Chose it for
persona adherence under pressure — staying terse and not sliding into assistant
register when interrupted, which is what "sounds like a real patient" is graded
on. `LLM_PROVIDER=openai` switches to gpt-4o-mini; both plugins are installed so
the swap is real and testable in Phase 2. `max_tokens=120` is a hard brake on
rambling; a long turn is the clearest tell that a bot is talking.

**Turn detection: `inference.TurnDetector()` (audio-based).**
The `livekit-plugins-turn-detector` text model is now deprecated upstream in
favour of this. Audio-based means it reads acoustics as well as text, which
should handle the mumbler and long-silence personas better. `TURN_DETECTOR=local`
falls back to the on-device ONNX model at zero per-minute cost.

**TTS: Cartesia Sonic-3.** Lower time-to-first-byte than ElevenLabs and cheaper.
`TTS_PROVIDER=elevenlabs` swaps in Flash v2.5.

### Architecture

- **The agent dials, not the runner.** `ctx.add_sip_participant()` is called from
  inside the job entrypoint, so there's no window where the call is answered but
  the agent isn't listening yet.
- **One dial site, one hardcoded number.** `TARGET_NUMBER` lives only in
  `src/config.py`, is not read from env and not overridable by a flag.
  `assert_allowed_number()` is called before the session even starts, and
  `preflight.py` asserts that there is exactly one `add_sip_participant` call
  site in the tree and that the literal appears nowhere else.
- **`preflight.py`** added beyond the original plan. Every call costs money and
  counts as a test against their receptionist, so failing on a missing key after
  dialing would be wasteful. It checks env completeness, provider construction,
  scenario parsing and the dial guard without spending anything.
- **`run_calls.py` starts the worker itself** as a subprocess so the whole thing
  really is one command, and requires an interactive confirmation (or `--yes`)
  before dialing.

### Fixes during scaffolding

- Dial guard rejected `(805) 439-8008` — the same number in national format —
  while its docstring claimed format-insensitivity. Now normalises 10-digit NANP
  input and compares on digits.
- `_mmss(59.95)` rendered as `00:60.0`. Rounds to tenths before splitting.
- Scenario 12's note claimed a silence hook that didn't exist. Corrected to say
  it's prompt-driven for now and flagged for Phase 2.
- Real barge-in can't come from a prompt instruction alone — the framework only
  speaks after it detects a turn. Added a `speak_first_after` scenario field that
  starts talking over the greeting on a timer. Only scenario 06 uses it.
- `pyyaml==6.0.2` conflicted with livekit-agents' `>=6.0.3`; unpinned the
  non-livekit deps.

### Starting endpointing values (untuned — these are guesses, not answers)

| Knob | Value | Reasoning |
|---|---|---|
| `ENDPOINTING_MIN_DELAY` | 0.35s | Enough not to clip the receptionist mid-breath |
| `ENDPOINTING_MAX_DELAY` | 3.0s | Cap on waiting when it trails off |
| `INTERRUPTION_MIN_DURATION` | 0.4s | Above the length of a "mhm" backchannel |
| `INTERRUPTION_MIN_WORDS` | 2 | One stray STT token shouldn't cut them off |

Preemptive generation is available and left **off** for the first call, so the
baseline is simple. It's the first thing to try if pacing feels slow.

### Not yet verified

Nothing has been dialed. Everything above the network boundary is tested;
nothing below it is. Unknowns until call 1: whether the Twilio trunk
authenticates, how the receptionist's greeting interacts with our endpointing,
and whether `inference.TurnDetector` behaves on 8 kHz telephony audio.
