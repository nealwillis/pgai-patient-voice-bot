# Patient voice bot — Pretty Good AI test harness

A voice bot that **calls** Pretty Good AI's test line and role-plays patients to
test their AI receptionist. Python + LiveKit Agents in pipeline mode: separate
STT, LLM and TTS, no speech-to-speech model, no hosted voice platform.

- **14 patient personas** in [`scenarios/`](scenarios/) as editable YAML
- **Stereo recordings** in [`recordings/`](recordings/) — far end on channel 0, our
  patient on channel 1, so overlaps are audible and measurable
- **Timestamped transcripts** in [`transcripts/`](transcripts/)
- **Findings** in [`BUG_REPORT.md`](BUG_REPORT.md), generated then hand-reviewed
- Design rationale in [`ARCHITECTURE.md`](ARCHITECTURE.md), iteration log in
  [`CHANGELOG.md`](CHANGELOG.md)
- Diagrams: [`docs/overview-simple.svg`](docs/overview-simple.svg) for a general
  audience, [`docs/architecture.svg`](docs/architecture.svg) for the technical flow

> **It only ever dials +1-805-439-8008.** The number is a hardcoded constant in
> [`src/config.py`](src/config.py), not read from the environment and not
> overridable by a flag. `assert_allowed_number()` guards the single code path
> that can dial, and `preflight.py` asserts there is exactly one
> `add_sip_participant` call site in the tree and that the literal appears
> nowhere else.

## Setup

### 1. Install

Python 3.10–3.13 (developed and tested on 3.13).

```bash
python -m venv .venv
```

```bash
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

On macOS or Linux use `.venv/bin/python` throughout instead. Every script works
from any working directory, so you do not need to be in the project root.

### 2. Accounts

| Service | What you need |
|---|---|
| **LiveKit Cloud** | Project URL, API key, API secret. The free Build tier covers this project. |
| **Twilio** | A **funded** account (a trial account cannot SIP-dial an unverified number — error 32100). One voice-capable US local number, an Elastic SIP Trunk, a Termination SIP URI, and a Credential List username/password. |
| **Deepgram** | API key for nova-3. *Optional* — set `STT_PROVIDER=livekit` to route the same model through LiveKit instead. |
| **Cartesia** | API key and a voice ID. |
| **Anthropic** | API key, used for the patient LLM and the analyzer. |

Twilio setup: buy or reuse a voice-capable number → Elastic SIP Trunking → create
a trunk → **Termination** tab, set a Termination SIP URI → **Authentication →
Credential Lists**, create one and attach it → **Numbers**, add your number.
Leave Origination empty; it is inbound-only and unused here.

### 3. Configure

```bash
cp .env.example .env
```

On Windows `cmd.exe` use `copy .env.example .env`; PowerShell and Git Bash both
accept the line above.

Fill it in, then create the LiveKit trunk (idempotent — safe to re-run):

```bash
.venv/Scripts/python.exe setup_trunk.py
```

Paste the trunk id it prints back into `.env` as `LIVEKIT_SIP_TRUNK_ID`.

### 4. Pre-download the models

```bash
.venv/Scripts/python.exe -m src.patient download-files
```

Fetches the Silero VAD and turn-detector weights. Skipping this works, but the
files would then download during your **first paid call**, which risks stalling or
failing it. Do it once, up front.

### 5. Check before spending anything

```bash
.venv/Scripts/python.exe preflight.py
```

This verifies `.env` is complete, the scenarios parse, every provider client and
the real `AgentSession` construct, and the dial guard still holds — all without
placing a call. Do not run calls until it prints `PREFLIGHT PASSED`.

## Run

One command places every call and collects every artifact:

```bash
.venv/Scripts/python.exe run_calls.py --all
```

It asks for confirmation first, showing the number and estimated cost. Other forms:

```bash
.venv/Scripts/python.exe run_calls.py --list
```

```bash
.venv/Scripts/python.exe run_calls.py --scenario refill
```

Add `--yes` to skip the prompt. The runner starts the agent worker itself, so
there is no second process to manage. Calls are placed one at a time with a pause
between them, each is capped at three minutes, and a scenario that produces no
transcript is retried once. Call numbering never reuses an index, so re-running
never overwrites a call you already paid for.

## Analyze

```bash
.venv/Scripts/python.exe analyze.py
```

Measures each call's pacing from its audio, asks Claude Opus 5 to find defects,
and writes `BUG_REPORT.md`. Every finding is a self-contained block between
`<!-- finding -->` markers, so you can delete, reword or re-rank freely; your
edited copy is moved to `BUG_REPORT.prev.md` rather than overwritten. Use
`--no-llm` for pacing only with no API spend, or `--call 03` for a single call.

## Audit the deliverables

```bash
.venv/Scripts/python.exe audit_deliverables.py
```

Lists every call with its transcript, audio file, format and duration, and flags
anything under a minute, over three minutes, missing a file, or that was a failed
connection rather than a real conversation. Exits non-zero if fewer than ten calls
are clean.

## Layout

```
scenarios/         14 personas + base_prompt.md (the persona rules — edit freely)
src/config.py      the dial guard, provider selection, and the tuning knobs
src/patient.py     the pipeline agent: STT -> LLM -> TTS, VAD + turn detector
src/scenarios.py   loads YAML personas into a system prompt
src/artifacts.py   writes the OGG and the timestamped transcript
src/pacing.py      measures latency/think time/talk-over from the stereo audio
setup_trunk.py     one-time LiveKit SIP trunk creation
preflight.py       checks everything that can be checked for free
run_calls.py       places the calls, collects the artifacts
analyze.py         transcripts + pacing -> BUG_REPORT.md
audit_deliverables.py  checks the calls meet the submission requirements
```

## Tuning

The knobs that matter are env vars, so you can change one per call and attribute
the result. Defaults are in [`src/config.py`](src/config.py):

| Variable | Default | Effect |
|---|---|---|
| `ENDPOINTING_MAX_DELAY` | `2.0` | Ceiling when the turn detector is unsure the far end finished. The single biggest lever on perceived lag. |
| `ENDPOINTING_MIN_DELAY` | `0.25` | Floor before we consider a turn over. |
| `PREEMPTIVE_GENERATION` | `1` | Generate and speak before the turn is confirmed. |
| `INTERRUPTION_MIN_DURATION` | `0.4` | How much speech counts as a barge-in, not a backchannel. |
| `STT_PROVIDER` / `LLM_PROVIDER` / `TTS_PROVIDER` | `deepgram` / `anthropic` / `cartesia` | Swap providers. |
| `TURN_DETECTOR` | `inference` | `local` for the free on-device model. |

Individual personas can override the voice with a `voice:` key in their YAML.

## Cost

Well under the $20 budget. Roughly $0.03 of Twilio per three-minute call, plus a
few cents of STT/TTS/LLM; LiveKit's free tier covers the SIP and session minutes
at this volume. The analyzer costs a few cents per call reviewed. Note Twilio's
minimum account funding is $20, but almost none of it is consumed.
