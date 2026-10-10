"""Score the active (or a given) policy once.

    uv run python -m evals.run [--policy v0.json] [--trials 2] [--only id1,id2]
"""

import argparse

from scheduler.config import POLICIES_DIR
from scheduler.policy import load_policy

from .runner import format_summary, run_suite


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", help="policy file in policies/ (default: ACTIVE)")
    ap.add_argument("--trials", type=int, default=2)
    ap.add_argument("--only", help="comma-separated scenario ids")
    args = ap.parse_args()

    policy = load_policy(POLICIES_DIR / args.policy if args.policy else None)
    run = run_suite(policy, trials=args.trials, only=args.only.split(",") if args.only else None)
    print(format_summary(run))
    for c in run["conversations"]:
        for k in c["checks"]:
            if not k["passed"]:
                print(f"  FAIL {c['scenario']} t{c['trial']} {k['kind']}:{k['id']}: {k['detail'][:150]}")
        if c["error"]:
            print(f"  ERROR {c['scenario']} t{c['trial']}: {c['error']}")


if __name__ == "__main__":
    main()
