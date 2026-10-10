"""The self-improvement loop.

    uv run python -m evals.loop [--iters 3] [--trials 2]

baseline eval -> for each iteration: optimizer proposes a rule patch from the
failures -> candidate re-runs the SAME scenarios -> regression gate ->
accept (becomes the new active policy) or reject (feedback goes to the next
attempt). Writes a before/after REPORT.md under runs/.
"""

import argparse
import json
import time

from scheduler.config import POLICIES_DIR, ROOT, RUNS_DIR
from scheduler.policy import active_policy_path, load_policy

from .improve import apply_patch, gate, next_version, propose_patch
from .runner import extend_run, format_summary, make_client, run_suite


def log(msg: str = "") -> None:
    print(msg, flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters", type=int, default=3, help="max optimizer attempts")
    ap.add_argument("--trials", type=int, default=2, help="runs per scenario (LLMs are noisy)")
    ap.add_argument("--tolerance", type=float, default=0.0, help="allowed per-scenario pass-rate drop")
    ap.add_argument("--confirm-trials", type=int, default=4,
                    help="extra trials, on BOTH policies, for every scenario whose pass rate changed")
    args = ap.parse_args()
    client = make_client()

    policy = load_policy()
    log(f"== Baseline: {active_policy_path().name} ==")
    base_run = run_suite(policy, trials=args.trials, label="baseline")
    log(format_summary(base_run))
    first_run, attempts, feedback = base_run, [], None

    for i in range(1, args.iters + 1):
        if base_run["summary"]["scenario_pass_rate"] == 1.0:
            log("\nAll scenarios pass; nothing to improve.")
            break
        log(f"\n== Attempt {i}: proposing patch from failures of v{policy['version']} ==")
        patch = propose_patch(policy, base_run, feedback, client)
        version = next_version()
        cand, notes = apply_patch(policy, patch, version)
        for d in patch.get("diagnosis", []):
            log(f"  diagnosis [{d.get('scenario')}]: {d.get('root_cause')}")
        for h in patch.get("suspected_harness_issues", []):
            log(f"  harness?  [{h.get('scenario')}]: {h.get('reason')}  (flagged for human review, not auto-fixed)")
        for ch in patch.get("changes", []):
            log(f"  change: {ch.get('op')} {ch.get('id', '')} {ch.get('text', '')}")
        for n in notes:
            log(f"  guard:  {n}")
        if cand["rules"] == policy["rules"]:
            feedback = "No valid changes were produced. " + " ".join(notes)
            attempts.append({"attempt": i, "version": version, "accepted": False, "reasons": [feedback], "patch": patch})
            continue

        cand_path = POLICIES_DIR / "candidates" / f"v{version}.json"
        cand_path.parent.mkdir(exist_ok=True)
        cand_path.write_text(json.dumps(cand, indent=2))

        log(f"\n== Re-running the same scenarios with candidate v{version} ==")
        cand_run = run_suite(cand, trials=args.trials, label="candidate")
        log(format_summary(cand_run))
        changed = [sid for sid, p in base_run["summary"]["per_scenario"].items()
                   if p["pass_rate"] != cand_run["summary"]["per_scenario"][sid]["pass_rate"]]
        if changed and args.confirm_trials:
            # With few trials a single unlucky sample looks like a 50-point swing. Re-measure every
            # scenario that moved, on both sides, and gate on the pooled numbers.
            log(f"\n== Confirming {len(changed)} changed scenario(s) with {args.confirm_trials} more trials "
                f"on v{policy['version']} and v{version}: {', '.join(changed)} ==")
            extend_run(base_run, policy, changed, args.confirm_trials)
            extend_run(cand_run, cand, changed, args.confirm_trials)
            for sid in changed:
                b, c = base_run["summary"]["per_scenario"][sid], cand_run["summary"]["per_scenario"][sid]
                log(f"  {sid:<38} v{policy['version']}: {b['pass_rate']:.0%} (n={b['trials']})   "
                    f"v{version}: {c['pass_rate']:.0%} (n={c['trials']})")
        accepted, reasons = gate(base_run, cand_run, args.tolerance)
        attempts.append({"attempt": i, "version": version, "accepted": accepted, "reasons": reasons,
                         "patch": patch, "before": base_run["summary"], "after": cand_run["summary"],
                         "run": cand_run["path"]})
        if accepted:
            (POLICIES_DIR / f"v{version}.json").write_text(json.dumps(cand, indent=2))
            cand_path.unlink()
            (POLICIES_DIR / "ACTIVE").write_text(f"v{version}.json\n")
            log(f"\nACCEPTED v{version}: now the active policy.")
            policy, base_run, feedback = cand, cand_run, None
        else:
            log(f"\nREJECTED v{version}: " + "; ".join(reasons))
            # Don't leak which held-out scenario regressed, or the holdout stops being a holdout.
            hidden = [sid for sid, p in base_run["summary"]["per_scenario"].items() if p["holdout"]]
            shown = "; ".join(reasons)
            for sid in hidden:
                shown = shown.replace(sid, "<a held-out scenario>")
            feedback = f"Changes tried: {json.dumps(patch.get('changes', []))}\nGate result: {shown}"

    path = write_report(first_run, base_run, attempts)
    log(f"\nReport: {path}")


def write_report(first: dict, final: dict, attempts: list) -> str:
    f, l = first["summary"], final["summary"]
    rows = ["| scenario | before (v%d) | after (v%d) | change |" % (first["policy_version"], final["policy_version"]),
            "|---|---|---|---|"]
    for sid, p in f["per_scenario"].items():
        a = l["per_scenario"][sid]["pass_rate"]
        delta = a - p["pass_rate"]
        mark = "fixed" if delta > 0 else ("REGRESSED" if delta < 0 else "")
        rows.append(f"| {sid}{' (holdout)' if p['holdout'] else ''} | {p['pass_rate']:.0%} | {a:.0%} | {mark} |")
    for key, name in [("scenario_pass_rate", "**Scenario pass rate**"), ("check_pass_rate", "Check pass rate"),
                      ("safety_check_pass_rate", "Safety check pass rate")]:
        rows.append(f"| {name} | {f[key]:.0%} | {l[key]:.0%} | {l[key] - f[key]:+.0%} |")

    out = [f"# Improvement loop report ({time.strftime('%Y-%m-%d %H:%M')})", "",
           f"Trials per scenario: {first['trials']}. Baseline run: `{first['path']}`. Final run: `{final['path']}`.",
           "", *rows, "", "## Attempts", ""]
    for a in attempts:
        out.append(f"### Attempt {a['attempt']} -> v{a['version']}: {'ACCEPTED' if a['accepted'] else 'REJECTED'}")
        for r in a["reasons"]:
            out.append(f"- gate: {r}")
        for d in a["patch"].get("diagnosis", []):
            out.append(f"- diagnosis [{d.get('scenario')}]: {d.get('root_cause')}")
        for h in a["patch"].get("suspected_harness_issues", []):
            out.append(f"- suspected harness issue [{h.get('scenario')}]: {h.get('reason')}")
        for ch in a["patch"].get("changes", []):
            out.append(f"- `{ch.get('op')} {ch.get('id', '')}` {ch.get('text', '')}  \n  _why:_ {ch.get('rationale', '')}")
        out.append("")
    bf, bl = f["blind_judge"], l["blind_judge"]
    out += ["## Transcript-only judge blind spot", "",
            f"A judge that only reads the chat said 'handled' while the clinic DB disagreed in "
            f"{bf['fooled']}/{bf['compared']} transactional conversations before and {bl['fooled']}/{bl['compared']} after."]
    d = RUNS_DIR / f"loop_{time.strftime('%Y%m%d-%H%M%S')}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "REPORT.md").write_text("\n".join(out))
    return str((d / "REPORT.md").relative_to(ROOT))


if __name__ == "__main__":
    main()
