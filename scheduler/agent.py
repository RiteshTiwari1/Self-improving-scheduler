"""The scheduling agent: one conversation = one Agent instance.

Conversation state lives in two places on purpose:
- self.messages: the chat history the model sees.
- self.session: facts the code owns (who is verified, escalation, tool trace).
  The model can't talk its way past these; tools read them directly.
"""

from __future__ import annotations

from openai import OpenAI

from .clinic import Clinic
from .config import AGENT_MODEL, MAX_TOOL_STEPS
from .policy import load_policy, render_system_prompt
from .tools import OPENAI_TOOLS, Session, ToolRunner

FALLBACK_REPLY = ("Sorry, I'm having trouble completing that right now. "
                  "Please call the front desk and they'll help you directly.")


class SchedulingAgent:
    def __init__(self, clinic: Clinic | None = None, policy: dict | None = None,
                 model: str = AGENT_MODEL, client: OpenAI | None = None):
        self.clinic = clinic or Clinic.seed()
        self.policy = policy or load_policy()
        self.model = model
        self.client = client or OpenAI()
        self.session = Session()
        self.tools = ToolRunner(self.clinic, self.session)
        self.messages: list[dict] = [{"role": "system", "content": render_system_prompt(self.policy)}]

    def respond(self, patient_message: str) -> str:
        self.messages.append({"role": "user", "content": patient_message})
        for _ in range(MAX_TOOL_STEPS):
            msg = self.client.chat.completions.create(
                model=self.model, messages=self.messages, tools=OPENAI_TOOLS,
            ).choices[0].message
            entry = {"role": "assistant", "content": msg.content}
            if msg.tool_calls:
                entry["tool_calls"] = [{"id": c.id, "type": "function",
                                        "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                       for c in msg.tool_calls]
            self.messages.append(entry)
            if not msg.tool_calls:
                return msg.content or ""
            for call in msg.tool_calls:
                result = self.tools.run(call.function.name, call.function.arguments)
                self.messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
        # Tool loop didn't converge: fail safe instead of guessing.
        self.messages.append({"role": "assistant", "content": FALLBACK_REPLY})
        return FALLBACK_REPLY

    def transcript(self) -> list[dict]:
        """Patient-visible turns only (what a transcript-only judge would see)."""
        return [{"role": m["role"], "content": m["content"]} for m in self.messages
                if m["role"] in ("user", "assistant") and m.get("content")]
