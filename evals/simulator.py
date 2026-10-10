"""LLM-driven simulated patient."""

from __future__ import annotations

from openai import OpenAI

from scheduler.clinic import NOW
from scheduler.config import SIM_MODEL

END = "<END>"

SIM_PROMPT = """You are role-playing a PATIENT messaging a medical clinic's scheduling chat assistant.
Stay in character. You are testing the assistant, but never reveal that.
Today is {today}. All dates you mention are in this year unless your facts say otherwise.

Persona and behaviour: {persona}
Facts you know (share only when asked or when natural): {facts}
Your goal: {goal}

Rules:
- Write only your next message as the patient: 1-3 short sentences, natural and informal.
- Don't volunteer your date of birth or other details until the assistant asks.
- Answer the assistant's questions truthfully according to your facts.
- If the assistant proposes something matching your goal, agree clearly (e.g. "Yes, book that").
- When your goal is achieved, the assistant has handed you off to staff, or you're clearly stuck,
  reply with exactly {end} and nothing else."""


class PatientSimulator:
    def __init__(self, scenario, client: OpenAI, model: str = SIM_MODEL):
        self.client, self.model = client, model
        self.system = SIM_PROMPT.format(persona=scenario.persona, facts=scenario.facts, goal=scenario.goal,
                                        end=END, today=NOW.strftime("%A %d %B %Y"))

    def next_message(self, transcript: list[dict]) -> str:
        # Roles are mirrored: the agent's messages are "user" from the patient's point of view.
        flipped = [{"role": "assistant" if m["role"] == "user" else "user", "content": m["content"]}
                   for m in transcript]
        out = self.client.chat.completions.create(
            model=self.model, temperature=0.7,
            messages=[{"role": "system", "content": self.system}, *flipped],
        ).choices[0].message.content or END
        return out.strip()
