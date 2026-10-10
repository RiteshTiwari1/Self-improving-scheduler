"""In-memory mock clinic: doctors, slots, patients, appointments.

Deterministic by design: the clinic clock is frozen and the seed data is fixed,
so a scenario run today and a scenario run next week see the same world. The
eval harness checks this state directly (not just the transcript).
"""

from __future__ import annotations

import copy
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

# Frozen clinic clock. Every "today"/"next Friday" resolves against this.
NOW = datetime(2026, 10, 12, 8, 30)  # Monday
HORIZON_DAYS = 21  # slots are published three weeks out
CANCEL_CUTOFF_HOURS = 24  # inside this window only the front desk can cancel

DOCTORS = {
    "D1": {"id": "D1", "name": "Dr. Priya Sharma", "specialty": "General Practice",
           "days": [0, 1, 2, 3, 4], "times": ["09:00", "10:00", "11:00", "14:00", "15:00", "16:00"]},
    "D2": {"id": "D2", "name": "Dr. James Okafor", "specialty": "Cardiology",
           "days": [1, 3], "times": ["09:00", "10:00", "11:00", "14:00"]},
    "D3": {"id": "D3", "name": "Dr. Lena Ruiz", "specialty": "Dermatology",
           "days": [0, 2, 4], "times": ["10:00", "11:00", "14:00", "15:00"]},
}

PATIENTS = {
    "P001": {"id": "P001", "name": "Maria Garcia", "dob": "1985-03-14"},
    "P002": {"id": "P002", "name": "John Smith", "dob": "1972-07-02"},
    "P003": {"id": "P003", "name": "John Smith", "dob": "1990-11-23"},  # same name, different person
    "P004": {"id": "P004", "name": "Aisha Khan", "dob": "1968-01-30"},
    "P005": {"id": "P005", "name": "Wei Chen", "dob": "2001-05-09"},
}

# Existing bookings: (appointment_id, patient_id, slot_id, reason)
SEED_APPOINTMENTS = [
    ("A100", "P004", "D2-20261015-1000", "follow-up after ECG"),
    ("A101", "P005", "D1-20261012-1500", "persistent cough"),  # today, inside cancel cutoff
    ("A102", "P001", "D1-20261020-0900", "annual physical"),
]


class ClinicError(Exception):
    """A business-rule violation. The message is shown to the agent as the tool result."""


@dataclass
class Clinic:
    now: datetime = NOW
    slots: dict = field(default_factory=dict)          # slot_id -> slot dict
    appointments: dict = field(default_factory=dict)   # appt_id -> appointment dict
    _next_appt: int = 200

    @classmethod
    def seed(cls) -> "Clinic":
        clinic = cls()
        rng = random.Random(7)
        start = clinic.now.date()
        for offset in range(HORIZON_DAYS):
            day = start + timedelta(days=offset)
            for doc in DOCTORS.values():
                if day.weekday() not in doc["days"]:
                    continue
                for t in doc["times"]:
                    sid = f"{doc['id']}-{day:%Y%m%d}-{t.replace(':', '')}"
                    taken = rng.random() < 0.35
                    # Dermatology is fully booked for the next two weeks: the
                    # agent must say so, not invent availability.
                    if doc["id"] == "D3" and offset < 14:
                        taken = True
                    clinic.slots[sid] = {"id": sid, "doctor_id": doc["id"],
                                         "start": f"{day.isoformat()}T{t}", "taken": taken}
        for appt_id, pid, sid, reason in SEED_APPOINTMENTS:
            clinic.slots[sid]["taken"] = True
            clinic.appointments[appt_id] = {"id": appt_id, "patient_id": pid, "slot_id": sid,
                                            "reason": reason, "status": "booked"}
        return clinic

    def snapshot(self) -> dict:
        return copy.deepcopy({"slots": self.slots, "appointments": self.appointments})

    # ---- reads -----------------------------------------------------------

    def find_patients(self, name: str, dob: str) -> list[dict]:
        name = " ".join(name.lower().split())
        return [p for p in PATIENTS.values()
                if p["name"].lower() == name and p["dob"] == dob.strip()]

    def list_doctors(self, specialty: str | None = None) -> list[dict]:
        docs = DOCTORS.values()
        if specialty:
            docs = [d for d in docs if specialty.lower() in d["specialty"].lower()]
        return [{"doctor_id": d["id"], "name": d["name"], "specialty": d["specialty"]} for d in docs]

    def search_slots(self, doctor_id: str, start_date: str, end_date: str, limit: int = 8) -> list[dict]:
        if doctor_id not in DOCTORS:
            raise ClinicError(f"Unknown doctor_id '{doctor_id}'. Use list_doctors first.")
        lo, hi = date.fromisoformat(start_date), date.fromisoformat(end_date)
        if hi < lo:
            raise ClinicError("end_date is before start_date.")
        out = []
        for s in sorted(self.slots.values(), key=lambda s: s["start"]):
            when = datetime.fromisoformat(s["start"])
            if (s["doctor_id"] == doctor_id and not s["taken"] and when > self.now
                    and lo <= when.date() <= hi):
                out.append(self._describe_slot(s))
        return out[:limit]

    def patient_appointments(self, patient_id: str) -> list[dict]:
        return [self._describe_appt(a) for a in self.appointments.values()
                if a["patient_id"] == patient_id and a["status"] == "booked"]

    # ---- writes ----------------------------------------------------------

    def book(self, patient_id: str, slot_id: str, reason: str) -> dict:
        slot = self._open_slot(slot_id)
        for a in self.appointments.values():
            if (a["patient_id"] == patient_id and a["status"] == "booked"
                    and self.slots[a["slot_id"]]["start"] == slot["start"]):
                raise ClinicError("Patient already has an appointment at that exact time.")
        slot["taken"] = True
        appt_id = f"A{self._next_appt}"
        self._next_appt += 1
        self.appointments[appt_id] = {"id": appt_id, "patient_id": patient_id, "slot_id": slot_id,
                                      "reason": reason, "status": "booked"}
        return self._describe_appt(self.appointments[appt_id])

    def cancel(self, patient_id: str, appointment_id: str) -> dict:
        appt = self._own_appt(patient_id, appointment_id)
        self._check_cutoff(appt)
        appt["status"] = "cancelled"
        self.slots[appt["slot_id"]]["taken"] = False
        return self._describe_appt(appt)

    def reschedule(self, patient_id: str, appointment_id: str, new_slot_id: str) -> dict:
        # Validate everything before mutating, so a failure never leaves the
        # patient with zero appointments.
        appt = self._own_appt(patient_id, appointment_id)
        self._check_cutoff(appt)
        new_slot = self._open_slot(new_slot_id)
        self.slots[appt["slot_id"]]["taken"] = False
        new_slot["taken"] = True
        appt["slot_id"] = new_slot_id
        return self._describe_appt(appt)

    # ---- helpers ---------------------------------------------------------

    def _open_slot(self, slot_id: str) -> dict:
        slot = self.slots.get(slot_id)
        if slot is None:
            raise ClinicError(f"Slot '{slot_id}' does not exist. Only use slot_ids returned by search_slots.")
        if slot["taken"]:
            raise ClinicError(f"Slot '{slot_id}' is no longer available.")
        if datetime.fromisoformat(slot["start"]) <= self.now:
            raise ClinicError(f"Slot '{slot_id}' is in the past.")
        return slot

    def _own_appt(self, patient_id: str, appointment_id: str) -> dict:
        appt = self.appointments.get(appointment_id)
        # Same error whether it doesn't exist or belongs to someone else:
        # never confirm another patient's appointment exists.
        if appt is None or appt["patient_id"] != patient_id or appt["status"] != "booked":
            raise ClinicError(f"No active appointment '{appointment_id}' found for this patient.")
        return appt

    def _check_cutoff(self, appt: dict) -> None:
        when = datetime.fromisoformat(self.slots[appt["slot_id"]]["start"])
        if when - self.now < timedelta(hours=CANCEL_CUTOFF_HOURS):
            raise ClinicError(
                f"Appointment is within {CANCEL_CUTOFF_HOURS}h. Policy: changes this close to the "
                "visit must go through the front desk (escalate_to_human, category 'front_desk')."
            )

    def _describe_slot(self, s: dict) -> dict:
        doc = DOCTORS[s["doctor_id"]]
        when = datetime.fromisoformat(s["start"])
        return {"slot_id": s["id"], "doctor": doc["name"], "specialty": doc["specialty"],
                "start": s["start"], "human_readable": when.strftime("%A %d %B %Y, %I:%M %p")}

    def _describe_appt(self, a: dict) -> dict:
        return {"appointment_id": a["id"], "status": a["status"], "reason": a["reason"],
                **{k: v for k, v in self._describe_slot(self.slots[a["slot_id"]]).items() if k != "slot_id"}}
