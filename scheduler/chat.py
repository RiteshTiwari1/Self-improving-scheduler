"""Interactive CLI: talk to the agent as a patient.

    uv run python -m scheduler.chat [--debug]

--debug prints each tool call and its result so you can see what the agent
actually did, not just what it said.
"""

import argparse
import json

from .agent import SchedulingAgent
from .policy import active_policy_path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true", help="show tool calls")
    args = ap.parse_args()

    agent = SchedulingAgent()
    print(f"Riverside Family Clinic scheduling assistant  (policy: {active_policy_path().name}, model: {agent.model})")
    print("Type your message. Ctrl-C or 'quit' to exit.\n")
    seen = 0
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if text.lower() in {"quit", "exit"}:
            break
        if not text:
            continue
        reply = agent.respond(text)
        if args.debug:
            for t in agent.session.trace[seen:]:
                mark = "ok " if t["ok"] else "ERR"
                print(f"  [{mark}] {t['tool']}({json.dumps(t['args'])}) -> {json.dumps(t['result'])[:200]}")
            seen = len(agent.session.trace)
        print(f"agent> {reply}\n")


if __name__ == "__main__":
    main()
