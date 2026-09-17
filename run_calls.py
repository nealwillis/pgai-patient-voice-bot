"""Place calls to the Pretty Good AI test line and collect the artifacts.

    python run_calls.py --list
    python run_calls.py --scenario refill
    python run_calls.py --all

One command: this script starts the agent worker as a subprocess, dispatches a
job per scenario, waits for each call to finish, and leaves the OGG in
recordings/ and the transcript in transcripts/.

Every call costs money and counts as a test against their receptionist, so the
script always asks for confirmation first unless you pass --yes.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid

# Windows consoles default to cp1252 but worker logs are UTF-8, and Python
# block-buffers stdout when it is redirected to a file -- which hides all progress
# during a 45-minute run. Fix both before anything prints.
for _s in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

from livekit import api

from src.artifacts import next_call_index
from src.config import (
    MAX_CALL_SECONDS,
    PROJECT_ROOT,
    TARGET_NUMBER,
    TRANSCRIPTS_DIR,
    assert_allowed_number,
    require_env,
)
from src.patient import AGENT_NAME
from src.scenarios import Scenario, all_scenarios, load_scenario

WORKER_READY_TIMEOUT = 90       # seconds to wait for the worker to register
CALL_SLACK = 75                 # grace on top of MAX_CALL_SECONDS for dial + teardown
PAUSE_BETWEEN_CALLS = 10        # don't hammer their line back-to-back
SHUTDOWN_GRACE = 20            # let an in-flight call finish writing before we kill it
RETRIES_PER_CALL = 1           # one retry per scenario if it yields no transcript


# ---------------------------------------------------------------------------
# worker subprocess
# ---------------------------------------------------------------------------
# Framework chatter that tells us nothing about the call itself. The loop-blocked
# warnings matter when tuning, but they bury everything else when they fire.
NOISE = (
    "event loop blocked",
    "initializing job runner",
    "job runner initialized",
    "job executor is unresponsive",
)


class Worker:
    """Runs `python -m src.patient start` and waits until it registers.

    Output is pumped off the pipe by a reader thread so nothing here can block
    on readline(), and so the worker never stalls on a full stdout buffer.
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen[str] | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()

    def __enter__(self) -> Worker:
        return self.open()

    def open(self) -> Worker:
        print("Starting agent worker...")
        # Force UTF-8 on both ends of the pipe. Windows consoles default to
        # cp1252, and a single un-encodable character in a log line is otherwise
        # enough to kill the runner mid-call.
        child_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "src.patient", "start"],
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=child_env,
        )
        threading.Thread(target=self._pump, daemon=True).start()
        self._await_registration()
        return self

    def _pump(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            self._lines.put(line)
        self._lines.put(None)  # EOF

    def _next(self, timeout: float) -> str | None:
        try:
            return self._lines.get(timeout=timeout)
        except queue.Empty:
            return ""

    def _await_registration(self) -> None:
        deadline = time.monotonic() + WORKER_READY_TIMEOUT
        while time.monotonic() < deadline:
            line = self._next(timeout=2)
            if line is None:
                raise RuntimeError(
                    "worker exited before registering -- its output above usually "
                    "says why (missing key, bad LIVEKIT_URL)."
                )
            if line and not any(n in line for n in NOISE):
                sys.stdout.write(f"  [worker] {line}")
            if line and "registered worker" in line:
                print("Worker registered.\n")
                return
        raise TimeoutError(f"worker did not register within {WORKER_READY_TIMEOUT}s")

    def drain_output(self) -> None:
        """Echo everything the worker has said so far. Never blocks, never raises.

        Printing must not be able to abort a call in progress -- an exception
        here once killed the runner mid-call and cost us the recording.
        """
        while True:
            try:
                line = self._next(timeout=0.1)
                if line is None:
                    print("  [worker] (worker process exited)")
                    return
                if not line:
                    return
                if not any(n in line for n in NOISE):
                    sys.stdout.write(f"  [worker] {line}")
            except Exception as e:  # noqa: BLE001 -- logging must never be fatal
                print(f"  [worker] (could not print a log line: {type(e).__name__})")
                return

    def alive(self) -> bool:
        return bool(self._proc and self._proc.poll() is None)

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._proc:
            return
        # Grace period: if we're unwinding because of an error, a call may still
        # be finishing and writing its artifacts. Killing the worker now is how
        # we lost call 1's recording.
        print(f"\nLetting the worker settle for {SHUTDOWN_GRACE}s...")
        deadline = time.monotonic() + SHUTDOWN_GRACE
        while time.monotonic() < deadline and self.alive():
            self.drain_output()
            time.sleep(0.5)
        print("Stopping worker...")
        self._proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self._proc.wait(timeout=15)
        if self._proc.poll() is None:
            self._proc.kill()


# ---------------------------------------------------------------------------
# one call
# ---------------------------------------------------------------------------
async def place_call(
    lkapi: api.LiveKitAPI, worker: Worker, scenario: Scenario, index: int
) -> bool:
    room = f"pgai-{scenario.id}-{uuid.uuid4().hex[:8]}"
    transcript = TRANSCRIPTS_DIR / f"call-{index:02d}-{scenario.id}-transcript.txt"

    print(f"--- call {index:02d}: {scenario.id} ({scenario.label})")
    print(f"    persona {scenario.name}, room {room}")

    await lkapi.agent_dispatch.create_dispatch(
        api.CreateAgentDispatchRequest(
            agent_name=AGENT_NAME,
            room=room,
            metadata=json.dumps({"scenario": scenario.id, "call_index": index}),
        )
    )

    # The agent writes the transcript as soon as the call ends, so the file
    # appearing is the signal that the call is genuinely done.
    deadline = time.monotonic() + MAX_CALL_SECONDS + CALL_SLACK
    while time.monotonic() < deadline:
        worker.drain_output()  # stream the call as it happens
        if transcript.exists():
            print(f"    done -> {transcript.relative_to(PROJECT_ROOT)}")
            return True
        if not worker.alive():
            print("    worker process died mid-call -- see its output above")
            return False
        await asyncio.sleep(1)

    print(f"    TIMED OUT after {MAX_CALL_SECONDS + CALL_SLACK}s with no transcript")
    worker.drain_output()
    return False


async def run(scenarios: list[Scenario]) -> int:
    lkapi = api.LiveKitAPI(
        url=require_env("LIVEKIT_URL"),
        api_key=require_env("LIVEKIT_API_KEY"),
        api_secret=require_env("LIVEKIT_API_SECRET"),
    )
    failed: list[str] = []
    worker: Worker | None = None
    try:
        for n, scenario in enumerate(scenarios):
            # The worker died mid-call once, for reasons the logs didn't capture.
            # A 45-minute run shouldn't be lost to that, so restart and carry on
            # rather than aborting the remaining scenarios.
            if worker is None or not worker.alive():
                if worker is not None:
                    print("Worker is gone; restarting for the remaining calls.\n")
                    worker.close()
                worker = Worker().open()

            for attempt in range(1, RETRIES_PER_CALL + 2):
                if not worker.alive():
                    worker.close()
                    worker = Worker().open()
                if attempt > 1:
                    print(f"    retrying {scenario.id} (attempt {attempt})")
                if await place_call(lkapi, worker, scenario, next_call_index()):
                    break
            else:
                failed.append(scenario.id)

            if n < len(scenarios) - 1:
                print(f"    pausing {PAUSE_BETWEEN_CALLS}s\n")
                await asyncio.sleep(PAUSE_BETWEEN_CALLS)
    finally:
        if worker is not None:
            worker.close()
        await lkapi.aclose()

    if failed:
        print(f"\nScenarios with no transcript: {', '.join(failed)}")
    return len(failed)


def confirm(scenarios: list[Scenario]) -> bool:
    print()
    print(f"About to place {len(scenarios)} real call(s) to {TARGET_NUMBER}:")
    for s in scenarios:
        print(f"  - {s.id:16} {s.label}")
    est = len(scenarios) * 3 * 0.014
    print(f"\nUp to {len(scenarios) * 3} minutes of call time, roughly ${est:.2f} of Twilio.")
    return input("Proceed? [y/N] ").strip().lower() in {"y", "yes"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--scenario", help="scenario id to run (see --list)")
    g.add_argument("--all", action="store_true", help="run every scenario, in order")
    g.add_argument("--list", action="store_true", help="list scenarios and exit")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    if args.list:
        for s in all_scenarios():
            print(f"{s.id:16} {s.category:11} {s.label}")
        return 0

    scenarios = all_scenarios() if args.all else [load_scenario(args.scenario)]

    # Belt and braces: the agent guards its own dial path, but fail loudly here
    # too rather than spinning up a worker for a call that would be refused.
    assert_allowed_number(TARGET_NUMBER)

    if not args.yes and not confirm(scenarios):
        print("Aborted. No calls placed.")
        return 1

    failures = asyncio.run(run(scenarios))
    print()
    if failures:
        print(f"{failures} of {len(scenarios)} scenario(s) produced no transcript.")
        return 1
    print(f"All {len(scenarios)} scenario(s) completed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
