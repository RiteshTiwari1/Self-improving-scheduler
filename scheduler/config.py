import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

AGENT_MODEL = os.getenv("AGENT_MODEL", "gpt-4.1-mini")
SIM_MODEL = os.getenv("SIM_MODEL", "gpt-4.1-mini")            # simulated patient
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "gpt-4.1")              # rubric judge: stronger than the agent
OPTIMIZER_MODEL = os.getenv("OPTIMIZER_MODEL", "gpt-4.1")      # writes policy patches
MAX_TOOL_STEPS = 8  # per patient turn; stops runaway tool loops

PROMPTS_DIR = ROOT / "prompts"
POLICIES_DIR = ROOT / "policies"
RUNS_DIR = ROOT / "runs"
