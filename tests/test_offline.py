"""Offline tests: no API key needed.

Covers the code-enforced guardrails, the state checks, and the whole
eval -> patch -> gate pipeline driven by a scripted fake OpenAI client.
"""

import json
from types import SimpleNamespace as NS

from evals import checks as C
from evals.improve import apply_patch, gate
from evals.runner import run_conversation, summarize
from evals.scenarios import BY_ID
from scheduler.clinic import Clinic
from scheduler.tools import Session, ToolRunner


def runner():
    c, s = Clinic.seed(), Session()
    return c, s, ToolRunner(c, s)


def call(t, name, **args):
    return json.loads(t.run(name, json.dumps(args)))


# ---------------------------------------------------------------- guardrails

def test_writes_require_verification():
    _, _, t = runner()
    assert "error" in call(t, "book_appointment", slot_id="D1-20261013-1100", reason="x")


def test_verification_locks_after_three_failures():
    _, _, t = runner()
    for _ in range(3):
        call(t, "verify_patient", full_name="Maria Garcia", date_of_birth="1900-01-01")
    out = call(t, "verify_patient", full_name="Maria Garcia", date_of_birth="1985-03-14")
    assert "locked" in out["error"]


def test_cannot_touch_other_patients_appointment():
    _, _, t = runner()
    call(t, "verify_patient", full_name="Maria Garcia", date_of_birth="1985-03-14")
    out = call(t, "cancel_appointment", appointment_id="A100")  # Aisha's
    assert out["error"].startswith("No active appointment")   # same message as a missing id


def test_invented_and_taken_slots_rejected():
    _, _, t = runner()
    call(t, "verify_patient", full_name="Maria Garcia", date_of_birth="1985-03-14")
    assert "does not exist" in call(t, "book_appointment", slot_id="D1-20261013-0930", reason="x")["error"]
    assert "no longer available" in call(t, "book_appointment", slot_id="D3-20261014-1000", reason="x")["error"]


def test_cancel_inside_24h_blocked():
    c, _, t = runner()
    call(t, "verify_patient", full_name="Wei Chen", date_of_birth="2001-05-09")
    assert "24h" in call(t, "cancel_appointment", appointment_id="A101")["error"]
    assert c.appointments["A101"]["status"] == "booked"


# ---------------------------------------------------------------- state checks

def test_booked_check_and_slots_from_search():
    c, s, t = runner()
    call(t, "verify_patient", full_name="Maria Garcia", date_of_birth="1985-03-14")
    call(t, "book_appointment", slot_id="D1-20261013-1100", reason="sore throat")  # never searched
    ctx = C.Ctx(c, s)
    assert C.Booked("P001", "D1", dates=["2026-10-13"], before_hour=12)(ctx)[0]
    assert not C.SlotsFromSearch()(ctx)[0]


def test_mind_change_double_booking_fails_exactly_one():
    c, s, t = runner()
    call(t, "verify_patient", full_name="Maria Garcia", date_of_birth="1985-03-14")
    call(t, "book_appointment", slot_id="D1-20261014-0900", reason="checkup")
    call(t, "book_appointment", slot_id="D1-20261016-1400", reason="checkup")
    ok, detail = C.Booked("P001", "D1", dates=["2026-10-16"], after_hour=12)(C.Ctx(c, s))
    assert not ok and "exactly one" in detail


# ---------------------------------------------------------------- pipeline with a fake LLM

def _msg(content=None, tool_calls=None):
    return NS(choices=[NS(message=NS(content=content, tool_calls=tool_calls))])


def _tc(i, name, **args):
    return NS(id=f"c{i}", function=NS(name=name, arguments=json.dumps(args)))


class FakeClient:
    """Agent: verify, search, book on turn 2. Patient: agrees then ends. Judge: all 'no'."""

    def __init__(self):
        self.chat = NS(completions=NS(create=self.create))

    def create(self, model, messages, tools=None, response_format=None, **_):
        if response_format:  # judge
            if "handled" in messages[0]["content"]:
                return _msg(json.dumps({"handled": True, "reason": "agent said booked"}))
            return _msg(json.dumps({"answers": {}}))
        if tools:  # agent
            n_user = sum(m["role"] == "user" for m in messages)
            if messages[-1]["role"] == "tool":
                return _msg("Done, you're booked for Tuesday 11:00 with Dr. Sharma.")
            if n_user == 2:
                return _msg(tool_calls=[
                    _tc(1, "verify_patient", full_name="Maria Garcia", date_of_birth="1985-03-14"),
                    _tc(2, "search_slots", doctor_id="D1", start_date="2026-10-13", end_date="2026-10-13"),
                    _tc(3, "book_appointment", slot_id="D1-20261013-1100", reason="sore throat")])
            return _msg("Sure. Tuesday 11:00 with Dr. Sharma, shall I book it?")
        # patient simulator
        return _msg("<END>" if sum(m["role"] == "user" for m in messages) >= 2 else "Yes please, Maria Garcia, 1985-03-14.")


def test_conversation_pipeline_scores_state():
    policy = {"version": 0, "rules": []}
    conv = run_conversation(BY_ID["book_gp_tomorrow"], policy, 0, FakeClient())
    assert conv["error"] is None, conv.get("tb")
    state = {k["id"]: k["passed"] for k in conv["checks"] if k["kind"] == "state"}
    assert state == {"booked_correct_slot": True, "slots_from_search": True, "not_escalated": True}
    assert conv["blind"]["judge_says_handled"] and conv["blind"]["state_says_handled"]


def test_patch_guard_and_gate():
    policy = {"version": 0, "rules": []}
    patch = {"changes": [
        {"op": "add", "text": "If a patient describes emergency symptoms, tell them to call 911 first."},
        {"op": "add", "text": "Maria Garcia prefers mornings."},          # names a test patient
        {"op": "edit", "id": "R9", "text": "x"}]}                           # unknown rule
    new, notes = apply_patch(policy, patch, 1)
    assert [r["id"] for r in new["rules"]] == ["R1"] and len(notes) == 2

    def run(rates, safety=1.0):
        return {"summary": {"per_scenario": {k: {"pass_rate": v} for k, v in rates.items()},
                            "scenario_pass_rate": sum(rates.values()) / len(rates),
                            "safety_check_pass_rate": safety, "errors": 0}}
    assert gate(run({"a": 0.0, "b": 1.0}), run({"a": 1.0, "b": 1.0}))[0]
    ok, reasons = gate(run({"a": 0.0, "b": 1.0}), run({"a": 1.0, "b": 0.5}))
    assert not ok and any("regression: b" in r for r in reasons)
    assert not gate(run({"a": 0.5, "b": 1.0}), run({"a": 1.0, "b": 1.0}, safety=0.9))[0]


def test_summarize_counts_fooled_judge():
    convs = [{"scenario": "book_gp_tomorrow", "passed": False, "error": None, "checks": [],
              "blind": {"judge_says_handled": True, "state_says_handled": False}}]
    s = summarize(convs, [BY_ID["book_gp_tomorrow"]])
    assert s["blind_judge"]["fooled"] == 1
