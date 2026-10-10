"""Tools exposed to the agent, and the session-scoped dispatcher that runs them.

Scoping decisions (enforced in code, not left to the prompt):
- patient_id is never a tool argument. It comes from the session after
  verify_patient succeeds, so the model cannot act on someone else's record.
- Verification needs name AND date of birth; ambiguous matches are refused.
- After 3 failed verification attempts the session is locked.
- Writes only accept slot_ids that exist; the backend rejects invented ones.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from .clinic import Clinic, ClinicError

MAX_VERIFY_ATTEMPTS = 3


@dataclass
class Session:
    verified_patient_id: str | None = None
    failed_verifications: int = 0
    escalation: dict | None = None
    trace: list = field(default_factory=list)  # every tool call: name, args, result, ok


TOOL_SCHEMAS = [
    {"name": "verify_patient",
     "description": "Verify the caller's identity using full name and date of birth. Required before "
                    "viewing, booking, cancelling or rescheduling appointments.",
     "parameters": {"type": "object", "properties": {
         "full_name": {"type": "string"},
         "date_of_birth": {"type": "string", "description": "YYYY-MM-DD"}},
         "required": ["full_name", "date_of_birth"]}},
    {"name": "list_doctors",
     "description": "List clinic doctors, optionally filtered by specialty.",
     "parameters": {"type": "object", "properties": {
         "specialty": {"type": "string"}}}},
    {"name": "search_slots",
     "description": "Find open appointment slots for one doctor in a date range (inclusive).",
     "parameters": {"type": "object", "properties": {
         "doctor_id": {"type": "string"},
         "start_date": {"type": "string", "description": "YYYY-MM-DD"},
         "end_date": {"type": "string", "description": "YYYY-MM-DD"}},
         "required": ["doctor_id", "start_date", "end_date"]}},
    {"name": "get_my_appointments",
     "description": "List the verified patient's upcoming appointments.",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "book_appointment",
     "description": "Book a slot for the verified patient.",
     "parameters": {"type": "object", "properties": {
         "slot_id": {"type": "string"},
         "reason": {"type": "string", "description": "Short non-diagnostic reason for visit"}},
         "required": ["slot_id", "reason"]}},
    {"name": "cancel_appointment",
     "description": "Cancel one of the verified patient's appointments.",
     "parameters": {"type": "object", "properties": {
         "appointment_id": {"type": "string"}},
         "required": ["appointment_id"]}},
    {"name": "reschedule_appointment",
     "description": "Move one of the verified patient's appointments to a new open slot.",
     "parameters": {"type": "object", "properties": {
         "appointment_id": {"type": "string"},
         "new_slot_id": {"type": "string"}},
         "required": ["appointment_id", "new_slot_id"]}},
    {"name": "escalate_to_human",
     "description": "Hand the conversation to clinic staff.",
     "parameters": {"type": "object", "properties": {
         "category": {"type": "string", "enum": ["emergency", "clinical_question", "front_desk", "other"]},
         "summary": {"type": "string", "description": "What staff need to know"}},
         "required": ["category", "summary"]}},
]

OPENAI_TOOLS = [{"type": "function", "function": s} for s in TOOL_SCHEMAS]

NEEDS_VERIFICATION = {"get_my_appointments", "book_appointment", "cancel_appointment", "reschedule_appointment"}


class ToolRunner:
    def __init__(self, clinic: Clinic, session: Session):
        self.clinic = clinic
        self.session = session

    def run(self, name: str, raw_args: str) -> str:
        args = {"_raw": raw_args}
        try:
            args = json.loads(raw_args or "{}")
            result, ok = self._dispatch(name, args), True
        except (ClinicError, ValueError, TypeError, KeyError) as e:
            result, ok = {"error": str(e)}, False
        self.session.trace.append({"tool": name, "args": args, "result": result, "ok": ok})
        return json.dumps(result)

    def _dispatch(self, name: str, a: dict):
        s, c = self.session, self.clinic
        if name in NEEDS_VERIFICATION and s.verified_patient_id is None:
            raise ClinicError("Caller is not verified. Call verify_patient with full name and date of birth first.")

        if name == "verify_patient":
            if s.failed_verifications >= MAX_VERIFY_ATTEMPTS:
                raise ClinicError("Verification locked after too many failed attempts. "
                                  "Escalate to front_desk; do not retry.")
            matches = c.find_patients(a["full_name"], a["date_of_birth"])
            if len(matches) != 1:
                s.failed_verifications += 1
                left = MAX_VERIFY_ATTEMPTS - s.failed_verifications
                raise ClinicError(f"No unique patient matches that name and date of birth. {left} attempt(s) left.")
            s.verified_patient_id = matches[0]["id"]
            return {"verified": True, "first_name": matches[0]["name"].split()[0]}

        if name == "list_doctors":
            return c.list_doctors(a.get("specialty"))
        if name == "search_slots":
            slots = c.search_slots(a["doctor_id"], a["start_date"], a["end_date"])
            return slots or {"slots": [], "note": "No open slots in that range."}
        if name == "get_my_appointments":
            return c.patient_appointments(s.verified_patient_id)
        if name == "book_appointment":
            return c.book(s.verified_patient_id, a["slot_id"], a["reason"])
        if name == "cancel_appointment":
            return c.cancel(s.verified_patient_id, a["appointment_id"])
        if name == "reschedule_appointment":
            return c.reschedule(s.verified_patient_id, a["appointment_id"], a["new_slot_id"])
        if name == "escalate_to_human":
            s.escalation = {"category": a["category"], "summary": a["summary"],
                            "ticket": f"T-{uuid.uuid4().hex[:6].upper()}"}
            return {"handed_off": True, "ticket": s.escalation["ticket"]}
        raise ClinicError(f"Unknown tool '{name}'.")
