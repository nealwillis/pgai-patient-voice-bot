# Architecture

## How it works

`run_calls.py` starts a LiveKit agent worker as a subprocess, then creates one
agent dispatch per scenario, passing the scenario id and call number as job
metadata. The worker's entrypoint (`src/patient.py`) loads that persona from
`scenarios/*.yaml`, builds a **pipeline** `AgentSession` — Deepgram nova-3 for STT,
Claude Haiku 4.5 for the LLM, Cartesia Sonic-3 for TTS, with Silero VAD plus
LiveKit's audio turn-detector model for turn-taking — connects to a LiveKit Cloud
room, and then **places the call itself** with `ctx.add_sip_participant()` through a
LiveKit SIP outbound trunk that fronts a Twilio Elastic SIP trunk. The agent dials
rather than the runner because that removes any window in which the far end has
answered but the agent is not yet listening. When the call ends — either the
patient's `end_call` tool, the far end hanging up, or the 180-second cap — the
agent flushes the recorder and writes a stereo OGG into `recordings/` and a
timestamped transcript into `transcripts/`, then the runner moves to the next
scenario. `analyze.py` reads every transcript, measures the pacing of each call
from its audio, asks Claude Opus 5 for defects, and writes `BUG_REPORT.md`.

## Why these choices

Pipeline mode was required, but it is also what makes the interesting knobs
reachable: a speech-to-speech model would hide endpointing and interruption
behind a single opaque policy, and turn-taking is the thing being graded. Within
the pipeline, latency dominated every provider decision — **Deepgram nova-3**
speaks to Deepgram directly rather than through LiveKit's inference gateway to
save a network hop, and **Cartesia Sonic-3** has the lowest time-to-first-byte of
the natural-sounding options. The LLM is the exception: at roughly two cents a
call, cost was irrelevant and **Claude Haiku 4.5** was chosen for staying in
character under pressure — remaining terse and not sliding into assistant
register when interrupted — with `max_tokens=60` as a hard brake on the
paragraph-shaped answers that are the clearest tell of a bot. Turn-taking is
Silero VAD for speech boundaries plus LiveKit's **audio** turn detector, which
reads acoustics as well as text and so handles a caller trailing off better than
the older text-only model. The single most consequential number is
`ENDPOINTING_MAX_DELAY`: at its 3.0s default the bot answered quickly most of the
time and then stalled over five seconds when the detector was unsure, which
sounds more broken than being uniformly slow. Dropping it to 2.0s and enabling
preemptive generation took measured response latency from 2.8s to 1.7s median —
the trade being that preemptive generation occasionally discards work it started
too early.

## The phone connection

There are two trunk objects because each side only knows half the problem.
LiveKit needs a **SIP outbound trunk** to know where to send its INVITE and which
credentials to present; Twilio's **Elastic SIP Trunk** is what actually terminates
that SIP leg onto the PSTN and supplies a caller ID the network will accept.
`setup_trunk.py` creates the LiveKit half once and is idempotent, so the Twilio
termination URI and credential-list username/password are configured in exactly
one place. Two constraints fell out of this. First, Twilio **trial** accounts
cannot SIP-dial an unverified number (error 32100), and the target line cannot be
verified because verification requires entering a code on the answering end — so
the account has to be funded, even though the calls themselves cost pennies.
Second, the brief requires every call to originate from **one** number, and their
receptionist keys off caller ID: it recognised a name from an earlier call and
greeted later personas with it. That is why the base prompt teaches every patient
to correct a wrong name naturally rather than fight it — the constraint became a
test of whether they can override a caller-ID assumption.

## Turn-taking, and what it costs

The two components do different jobs: Silero VAD answers *is someone speaking*
cheaply and locally, while the turn detector answers the harder question *is the
turn actually over*. Splitting them is what lets endpointing wait through a
mid-sentence pause without waiting through a finished sentence. The interruption
thresholds exist for the opposite failure: `min_duration=0.4s` and `min_words=2`
stop a backchannel — an "mhm" or a single stray STT token — from registering as a
barge-in and cutting the other side off, while `resume_false_interruption` means a
misfire resumes the thought instead of abandoning it, because a real caller does
not drop their sentence.

The honest cost of this arrangement is that `inference.TurnDetector()` is
**hosted**, so every turn boundary carries a network round trip to LiveKit's
gateway on top of VAD. We accepted that because end-of-turn accuracy dominated
perceived quality — a wrong endpoint costs seconds, a round trip costs
milliseconds — and because the local ONNX alternative is both weaker on
trailing-off speech and deprecated upstream. `TURN_DETECTOR=local` removes the
round trip and the per-minute cost if that trade ever stops making sense.

## Recording

Recording deviates from the original plan. LiveKit Cloud egress cannot write
locally (*"a request that resolves to no storage fails"*), so it would have needed
an S3 or R2 bucket — but the agents framework already writes its own **stereo**
OGG/Opus file, with the far end on channel 0 and our patient on channel 1. That
removed an entire cloud dependency, and more importantly the channel separation
is what makes barge-in auditable: a room-composite mix flattens both speakers
together, whereas separate channels let `src/pacing.py` measure who held the floor
and hand the analyzer real numbers for response latency, think time and talk-over.
That module exists because inferring pacing from transcript timestamps gave a
wrong answer early on — the timestamps mark when a reply was generated, not when
audio reached the line.

## Alternatives considered

| Instead of | We could have | Why not |
|---|---|---|
| Twilio SIP trunk | LiveKit's own phone numbers | [Inbound only](https://docs.livekit.io/telephony/start/phone-numbers/) today |
| Framework-local OGG | LiveKit Egress → S3/R2 | Needs a bucket, and a composite mix loses the channel separation that makes overlap measurable |
| Deepgram direct | `inference.STT("deepgram/nova-3")` | Same model, one extra hop. Kept as `STT_PROVIDER=livekit` for anyone without a Deepgram key |
| Claude Haiku 4.5 | gpt-4o-mini | Cheaper still, but cost was already negligible. Kept as `LLM_PROVIDER=openai` |
| Cartesia Sonic-3 | ElevenLabs Flash v2.5 | Higher TTFB. Kept as `TTS_PROVIDER=elevenlabs` |
| Audio turn detector | Local ONNX text model | Free and offline, but deprecated upstream and weaker on trailing-off speech. Kept as `TURN_DETECTOR=local` |
| `endpointing.mode: fixed` | `dynamic` (EMA of the far end's pacing) | Plausibly better against a consistently slow receptionist, but six things changed in that round already and a seventh would have made attribution impossible. Untried, and the next lever |
| `nova-3` | `nova-2-phonecall` | Telephony-tuned; a live A/B candidate via `DEEPGRAM_MODEL` |

Every one of those alternatives is a single `.env` change, which is deliberate:
the whole point of Phase 2 was being able to change one variable per call and
attribute the result. See [CHANGELOG.md](CHANGELOG.md) for what actually moved.
