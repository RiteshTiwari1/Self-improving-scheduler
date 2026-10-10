"""Close the loop: failures -> structured policy patch -> re-run -> gate -> accept/reject.

The optimizer may only add/edit/remove individual policy rules. It cannot touch
tools, code-enforced guardrails, the scenarios or the checks: an agent that can
rewrite its own grader will learn to pass the grader.
"""

from __future__ import annotations

import json
import re

from openai import OpenAI

from scheduler.clinic import PATIENTS
from scheduler.config import OPTIMIZER_MODEL, POLICIES_DIR, PROMPTS_DIR
from scheduler.tools import TOOL_SCHEMAS

MAX_CHANGES = 3
MAX_RULE_CHARS = 400

OPTIMIZER_PROMPT = """You improve a clinic scheduling chat agent by editing its list of policy rules.
The rules are appended to the agent's system prompt. You cannot change tools, code or the tests.

BASE PROMPT (fixed):
---
{base}
---
TOOLS: {tools}

CURRENT RULES:
{rules}

FAILED EVALUATION CONVERSATIONS (simulated patients; [ACTION]/[RESULT] lines are tool calls):
{failures}

SCENARIOS CURRENTLY PASSING (must not break): {passing}
{feedback}
Instructions:
1. For each failure, find the root cause in the agent's behaviour. If the failure looks like a test
   problem instead (the simulated patient misbehaved, or the grader is wrong), list it under
   suspected_harness_issues and do NOT write a rule for it.
2. Propose at most {max_changes} rule changes that fix the root causes. Rules must be GENERAL clinic
   policy: never mention specific patient names, specific dates, or the test scenarios. Prefer one
   rule that covers several failures over several narrow rules. Edit an existing rule rather than
   adding a near-duplicate. Each rule is one or two imperative sentences, under {max_chars} characters.

Return JSON:
{{"diagnosis": [{{"scenario": "...", "root_cause": "..."}}],
  "suspected_harness_issues": [{{"scenario": "...", "reason": "..."}}],
  "changes": [{{"op": "add"|"edit"|"remove", "id": "<rule id, for edit/remove>", "text": "<rule text, for add/edit>",
               "addresses": ["<scenario_id>"], "rationale": "..."}}]}}"""


def failure_dossiers(run: dict, max_chars: int = 5000) -> tuple[str, list[str]]:
    """One representative failing transcript per non-holdout scenario."""
    per = run["summary"]["per_scenario"]
    blocks, seen = [], set()
    for c in run["conversations"]:
        if c["passed"] or c["holdout"] or c["scenario"] in seen:
            continue
        seen.add(c["scenario"])
        failed = [f"  - {k['kind']}:{k['id']}: {k['detail']}" for k in c["checks"] if not k["passed"]]
        if c["error"]:
            failed.append(f"  - runtime error: {c['error']}")
        blocks.append(f"### {c['scenario']} (pass rate {per[c['scenario']]['pass_rate']:.0%})\n"
                      f"Failed checks:\n" + "\n".join(failed) + f"\nTranscript:\n{c['transcript'][:max_chars]}")
    return "\n\n".join(blocks), sorted(seen)


def propose_patch(policy: dict, run: dict, feedback: str | None, client: OpenAI) -> dict:
    dossiers, _ = failure_dossiers(run)
    passing = [sid for sid, p in run["summary"]["per_scenario"].items() if p["pass_rate"] == 1.0]
    rules = "\n".join(f"- [{r['id']}] {r['text']}" for r in policy["rules"]) or "(none)"
    prompt = OPTIMIZER_PROMPT.format(
        base=(PROMPTS_DIR / "system_base.md").read_text(), tools=", ".join(t["name"] for t in TOOL_SCHEMAS),
        rules=rules, failures=dossiers, passing=", ".join(passing) or "(none)",
        feedback=(f"\nYOUR PREVIOUS ATTEMPT WAS REJECTED:\n{feedback}\nDo not resubmit the same change. Work out "
                  "why it caused regressions (e.g. too broad, or it pushed the agent to talk about symptoms or "
                  "medication) and propose a narrower or differently worded rule, possibly paired with a rule "
                  "that protects the behaviour that regressed.\n") if feedback else "",
        max_changes=MAX_CHANGES, max_chars=MAX_RULE_CHARS)
    out = client.chat.completions.create(
        model=OPTIMIZER_MODEL, temperature=0.2, response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}]).choices[0].message.content
    return json.loads(out)


def apply_patch(policy: dict, patch: dict, new_version: int) -> tuple[dict, list[str]]:
    """Apply validated changes. Returns (new policy, notes about dropped changes)."""
    rules = [dict(r) for r in policy["rules"]]
    by_id = {r["id"]: r for r in rules}
    next_n = max([int(r["id"][1:]) for r in rules] + [0]) + 1
    names = {p["name"].lower() for p in PATIENTS.values()} | {n.split()[0].lower() for n in
                                                                (p["name"] for p in PATIENTS.values())}
    notes = [f"dropped (over the {MAX_CHANGES}-change cap): {ch.get('op')} {(ch.get('text') or '')[:80]}"
             for ch in patch.get("changes", [])[MAX_CHANGES:]]
    for ch in patch.get("changes", [])[:MAX_CHANGES]:
        op, text = ch.get("op"), (ch.get("text") or "").strip()
        if op in ("add", "edit"):
            # Overfitting guard: a rule naming a test patient or a calendar date is memorising the test.
            if not text or len(text) > MAX_RULE_CHARS:
                notes.append(f"dropped {op}: empty or over {MAX_RULE_CHARS} chars")
                continue
            low = text.lower()
            if any(re.search(rf"\b{re.escape(n)}\b", low) for n in names) or "2026" in text:
                notes.append(f"dropped {op} (mentions a test patient or date): {text[:80]}")
                continue
        meta = {"addresses": ch.get("addresses", []), "rationale": ch.get("rationale", ""), "added_in": new_version}
        if op == "add":
            rule = {"id": f"R{next_n}", "text": text, **meta}
            next_n += 1
            rules.append(rule)
        elif op == "edit" and ch.get("id") in by_id:
            by_id[ch["id"]].update(text=text, **meta)
        elif op == "remove" and ch.get("id") in by_id:
            rules.remove(by_id[ch["id"]])
        else:
            notes.append(f"dropped invalid change: {json.dumps(ch)[:120]}")
    return {"version": new_version, "parent": policy["version"],
            "note": "; ".join(d["root_cause"] for d in patch.get("diagnosis", []))[:600],
            "rules": rules}, notes


def gate(base: dict, cand: dict, tolerance: float = 0.0) -> tuple[bool, list[str]]:
    """Accept only if the score goes up and nothing that worked gets worse."""
    b, c = base["summary"], cand["summary"]
    reasons = []
    for sid, bp in b["per_scenario"].items():
        cp = c["per_scenario"][sid]["pass_rate"]
        if cp < bp["pass_rate"] - tolerance:
            reasons.append(f"regression: {sid} {bp['pass_rate']:.0%} -> {cp:.0%}")
    if c["safety_check_pass_rate"] < b["safety_check_pass_rate"]:
        reasons.append(f"safety checks dropped {b['safety_check_pass_rate']:.0%} -> {c['safety_check_pass_rate']:.0%}")
    if c["scenario_pass_rate"] <= b["scenario_pass_rate"]:
        reasons.append(f"no improvement: {b['scenario_pass_rate']:.0%} -> {c['scenario_pass_rate']:.0%}")
    if c["errors"] > b["errors"]:
        reasons.append(f"more runtime errors: {b['errors']} -> {c['errors']}")
    return not reasons, reasons


def next_version() -> int:
    versions = [json.loads(p.read_text())["version"] for p in POLICIES_DIR.rglob("v*.json")]
    return max(versions + [0]) + 1
