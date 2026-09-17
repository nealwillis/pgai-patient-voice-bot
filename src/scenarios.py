"""Load patient personas from scenarios/*.yaml and turn them into a system prompt."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import SCENARIOS_DIR

BASE_PROMPT_PATH = SCENARIOS_DIR / "base_prompt.md"


@dataclass
class Scenario:
    id: str
    label: str
    category: str
    patient: dict
    reason: str
    goal: str
    personality: str
    tactics: list[str] = field(default_factory=list)
    success_criteria: list[str] = field(default_factory=list)
    notes: str = ""
    # Optional TTS voice id for this persona, so a voice can match the name.
    # Falls back to CARTESIA_VOICE_ID / ELEVENLABS_VOICE_ID.
    voice: str | None = None
    # BCP-47-ish language for this persona's speech. Anything other than "en"
    # also switches our STT to multilingual, so we still understand the far end
    # if they answer in English.
    language: str = "en"
    # Seconds after the call connects to start talking without waiting for the
    # far end to finish its greeting. None = normal turn-taking (the default).
    speak_first_after: float | None = None
    path: Path | None = None

    @property
    def name(self) -> str:
        return self.patient.get("name", "the caller")

    def instructions(self) -> str:
        """Base persona rules + this scenario, as one system prompt."""
        base = _read_base_prompt()
        tactics = "\n".join(f"- {t}" for t in self.tactics) or "- (none)"
        return f"""{base}

---

# Who you are on this call

Your name is {self.name}.
Your date of birth is {self.patient.get('dob', 'unknown')}.
Your callback number is {self.patient.get('callback', 'unknown')}.

## Why you are calling

The next few sections are your situation, written down for you. They are NOT
lines to read out. Nobody says a paragraph on the phone. Say the short spoken
version, a few words at a time, and only the part that answers what you were
just asked.

{self.reason.strip()}

## What you want

{self.goal.strip()}

## How you come across

{self.personality.strip()}

## Specific things you do on this call

{tactics}

Remember: short spoken sentences, let them finish, pursue the goal, then say
goodbye and end the call. Never break character.
{self._language_note()}"""

    def _language_note(self) -> str:
        """Extra instruction for a persona who does not speak English."""
        if self.language == "en":
            return ""
        lang = {"es": "Spanish"}.get(self.language, self.language)
        return (
            f"\n\nYou speak {lang}, not English. Every word you say is in {lang}.\n"
            f"If they answer in English, keep going in {lang} -- you do not speak\n"
            f"enough English to switch. If it becomes obvious they cannot understand\n"
            f"you, say so in {lang} and ask for someone who speaks it.\n"
        )


def _read_base_prompt() -> str:
    if not BASE_PROMPT_PATH.exists():
        raise FileNotFoundError(f"missing base prompt: {BASE_PROMPT_PATH}")
    return BASE_PROMPT_PATH.read_text(encoding="utf-8").split("---", 1)[-1].strip()


def _load_file(path: Path) -> Scenario:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    known = {f for f in Scenario.__dataclass_fields__ if f != "path"}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"{path.name}: unexpected keys {sorted(unknown)}")
    return Scenario(path=path, **data)


def all_scenarios() -> list[Scenario]:
    """Every scenario, in filename order (which is also our call order)."""
    files = sorted(SCENARIOS_DIR.glob("*.yaml"))
    if not files:
        raise FileNotFoundError(f"no scenario files in {SCENARIOS_DIR}")
    return [_load_file(p) for p in files]


def load_scenario(scenario_id: str) -> Scenario:
    for s in all_scenarios():
        if s.id == scenario_id:
            return s
    available = ", ".join(s.id for s in all_scenarios())
    raise KeyError(f"unknown scenario {scenario_id!r}. Available: {available}")
