"""Versioned policy: the base prompt plus a list of learned rules.

The improvement loop never rewrites the whole prompt. It adds, edits or removes
individual rules, each traceable to the failure that produced it, and saves the
result as a new version file. That keeps every change small, reviewable and
revertible.
"""

from __future__ import annotations

import json
from pathlib import Path

from .clinic import DOCTORS, NOW
from .config import POLICIES_DIR, PROMPTS_DIR


def active_policy_path() -> Path:
    return POLICIES_DIR / (POLICIES_DIR / "ACTIVE").read_text().strip()


def load_policy(path: Path | None = None) -> dict:
    return json.loads((path or active_policy_path()).read_text())


def render_system_prompt(policy: dict) -> str:
    doctors = "\n".join(f"- {d['name']} ({d['specialty']}), doctor_id {d['id']}" for d in DOCTORS.values())
    rules = ""
    if policy["rules"]:
        rules = "Clinic policies (follow these strictly):\n" + "\n".join(
            f"- [{r['id']}] {r['text']}" for r in policy["rules"])
    return (PROMPTS_DIR / "system_base.md").read_text().format(
        now=NOW.strftime("%A %d %B %Y, %H:%M"), doctors=doctors, rules=rules).strip()
