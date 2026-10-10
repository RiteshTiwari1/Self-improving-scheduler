.PHONY: setup chat eval loop test reset

setup:            ## install dependencies
	uv sync

chat:             ## talk to the agent (tool calls shown)
	uv run python -m scheduler.chat --debug

eval:             ## score the active policy once
	uv run python -m evals.run --trials 2

loop:             ## baseline -> improve -> re-run -> gate, writes runs/loop_*/REPORT.md
	uv run python -m evals.loop --iters 3 --trials 2

test:             ## offline tests, no API key needed
	uv run pytest -q

reset:            ## make v0 the active policy again (for re-demoing the loop)
	echo v0.json > policies/ACTIVE
