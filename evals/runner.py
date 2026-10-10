"""Run scenarios against a policy, score them, and save results."""

from __future__ import annotations

import json
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from openai import OpenAI

from scheduler.agent import SchedulingAgent
from scheduler.clinic import Clinic
from scheduler.config import ROOT, RUNS_DIR

from . import checks as C
from .judge import Judge, annotated_transcript
from .scenarios import BY_ID, GLOBAL_JUDGE, SCENARIOS, Scenario
from .simulator import END, PatientSimulator

# Scenarios whose success is a state change; only for these does a
# transcript-only "was it handled?" verdict have a ground truth to compare to.
TRANSACTIONAL = (C.Booked, C.Moved, C.Status)


def make_client() -> OpenAI:
    return OpenAI(max_retries=5, timeout=60)


def run_conversation(scenario: Scenario, policy: dict, trial: int, client: OpenAI) -> dict:
    agent = SchedulingAgent(clinic=Clinic.seed(), policy=policy, client=client)
    sim = PatientSimulator(scenario, client)
    out = {"scenario": scenario.id, "trial": trial, "holdout": scenario.holdout}
    try:
        patient_msg = scenario.opening
        for _ in range(scenario.max_turns):
            agent.respond(patient_msg)
            patient_msg = sim.next_message(agent.transcript())
            if END in patient_msg:
                break

        ctx = C.Ctx(clinic=agent.clinic, session=agent.session)
        results = []
        for chk in scenario.state_checks:
            ok, detail = chk(ctx)
            results.append({"id": chk.id, "kind": "state", "safety": chk.safety, "passed": ok, "detail": detail})
        results += Judge(client).grade(GLOBAL_JUDGE + scenario.judge_checks, agent.messages)

        blind = None
        if any(isinstance(c, TRANSACTIONAL) for c in scenario.state_checks):
            state_ok = all(r["passed"] for r in results if r["kind"] == "state")
            v = Judge(client).blind_verdict(scenario.goal, agent.messages)
            blind = {"judge_says_handled": bool(v.get("handled")), "state_says_handled": state_ok,
                     "reason": v.get("reason", "")}

        out.update(passed=all(r["passed"] for r in results), checks=results, blind=blind,
                   escalation=agent.session.escalation, error=None)
    except Exception as e:  # one broken conversation must not kill the suite
        out.update(passed=False, checks=[], blind=None, escalation=None,
                   error=f"{type(e).__name__}: {e}", tb=traceback.format_exc())
    out["transcript"] = annotated_transcript(agent.messages)
    out["messages"] = agent.messages[1:]  # drop system prompt; it's in the policy file
    return out


def summarize(convs: list[dict], scenarios: list[Scenario]) -> dict:
    per = {}
    for s in scenarios:
        rs = [c for c in convs if c["scenario"] == s.id]
        per[s.id] = {"title": s.title, "holdout": s.holdout, "trials": len(rs),
                     "pass_rate": sum(c["passed"] for c in rs) / len(rs)}
    all_checks = [k for c in convs for k in c["checks"]]
    safety = [k for k in all_checks if k["safety"]]
    blind = [c["blind"] for c in convs if c["blind"]]
    fooled = [b for b in blind if b["judge_says_handled"] and not b["state_says_handled"]]
    rate = lambda xs: sum(x["passed"] for x in xs) / len(xs) if xs else 1.0
    return {
        "scenario_pass_rate": sum(p["pass_rate"] for p in per.values()) / len(per),
        "check_pass_rate": rate(all_checks),
        "safety_check_pass_rate": rate(safety),
        "errors": sum(1 for c in convs if c["error"]),
        "per_scenario": per,
        "blind_judge": {"compared": len(blind), "fooled": len(fooled),
                        "disagreements": sum(b["judge_says_handled"] != b["state_says_handled"] for b in blind)},
    }


def run_suite(policy: dict, trials: int = 2, only: list[str] | None = None,
              workers: int = 8, label: str | None = None) -> dict:
    scenarios = [s for s in SCENARIOS if not only or s.id in only]
    client = make_client()
    jobs = [(s, t) for s in scenarios for t in range(trials)]
    with ThreadPoolExecutor(workers) as pool:
        convs = list(pool.map(lambda j: run_conversation(j[0], policy, j[1], client), jobs))
    run = {"policy_version": policy["version"], "trials": trials, "label": label,
           "summary": summarize(convs, scenarios), "conversations": convs}
    run["path"] = str(save_run(run).relative_to(ROOT))
    return run


def extend_run(run: dict, policy: dict, scenario_ids: list[str], extra_trials: int, workers: int = 8) -> dict:
    """Add more trials for some scenarios to an existing run and re-summarize (pooled)."""
    client = make_client()
    start = run["trials"]
    jobs = [(BY_ID[sid], start + t) for sid in scenario_ids for t in range(extra_trials)]
    with ThreadPoolExecutor(workers) as pool:
        run["conversations"] += list(pool.map(lambda j: run_conversation(j[0], policy, j[1], client), jobs))
    run["summary"] = summarize(run["conversations"], [BY_ID[sid] for sid in run["summary"]["per_scenario"]])
    run["confirmed"] = sorted(set(run.get("confirmed", [])) | set(scenario_ids))
    save_run(run, ROOT / run["path"])
    return run


def save_run(run: dict, d: Path | None = None) -> Path:
    d = d or RUNS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_v{run['policy_version']}{'_' + run['label'] if run['label'] else ''}"
    (d / "transcripts").mkdir(parents=True, exist_ok=True)
    (d / "results.json").write_text(json.dumps(run, indent=2, default=str))
    for c in run["conversations"]:
        lines = [f"# {c['scenario']} (trial {c['trial']}): {'PASS' if c['passed'] else 'FAIL'}", ""]
        if c["error"]:
            lines += [f"ERROR: {c['error']}", ""]
        lines += [f"- [{'x' if k['passed'] else ' '}] {k['kind']}:{k['id']}{' (safety)' if k['safety'] else ''}: "
                  f"{k['detail']}" for k in c["checks"]]
        lines += ["", "```", c["transcript"], "```"]
        (d / "transcripts" / f"{c['scenario']}_t{c['trial']}.md").write_text("\n".join(lines))
    return d


def format_summary(run: dict) -> str:
    s = run["summary"]
    lines = [f"Policy v{run['policy_version']}  |  trials/scenario: {run['trials']}  |  saved: {run.get('path', '')}",
             "", f"{'scenario':<38} {'pass':>6}"]
    for sid, p in s["per_scenario"].items():
        lines.append(f"{sid + (' (holdout)' if p['holdout'] else ''):<38} {p['pass_rate']:>6.0%}  (n={p['trials']})")
    b = s["blind_judge"]
    lines += ["",
              f"Scenario pass rate:     {s['scenario_pass_rate']:.0%}",
              f"Check pass rate:        {s['check_pass_rate']:.0%}",
              f"Safety check pass rate: {s['safety_check_pass_rate']:.0%}",
              f"Conversation errors:    {s['errors']}",
              f"Transcript-only judge fooled (said 'handled', state disagrees): {b['fooled']}/{b['compared']}"]
    return "\n".join(lines)
