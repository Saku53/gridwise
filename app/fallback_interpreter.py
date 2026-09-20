"""Outage-only interpreter. Used ONLY when Gemini is unreachable, never as the
primary path (phrase matching alone does not satisfy the LLM requirement)."""
from __future__ import annotations

import re

_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
          "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "noon": 12,
          "midnight": 0}
_FRACTIONS = {"half": 0.5, "halved": 0.5, "one-fifth": 0.2, "a fifth": 0.2,
              "one-quarter": 0.25, "a quarter": 0.25, "one-third": 0.33}


def _hours(text: str) -> list[int]:
    t = text.lower()
    nums = []
    for m in re.finditer(r"(\d{1,2})\s*(?::\d{2})?\s*(am|pm)?", t):
        v, ap = int(m.group(1)), m.group(2)
        if ap == "pm" and v < 12:
            v += 12
        if ap == "am" and v == 12:
            v = 0
        if 0 <= v <= 23:
            nums.append(v)
    for w, v in _WORDS.items():
        if re.search(rf"\b{w}\b", t):
            nums.append(v + 12 if "pm" in t and v < 12 else v)
    if len(nums) >= 2:
        a, b = nums[0], nums[1]
        if a < b:
            return list(range(a, b))       # end-exclusive
    return sorted(set(nums))[:1]


def _factor(text: str) -> float | None:
    t = text.lower()
    for k, v in _FRACTIONS.items():
        if k in t:
            return v
    m = re.search(r"(\d{1,3})\s*%", t)
    if m:
        p = int(m.group(1)) / 100.0
        return max(0.0, 1 - p) if re.search(r"(reduc|drop by|cut by|less)", t) else p
    return None


def regex_interpret(notes: list[str]) -> list[dict]:
    out = []
    for i, note in enumerate(notes):
        t = note.lower()
        hrs = _hours(note)
        d, adj = "no_op", None
        if hrs:
            if re.search(r"(solar|pv|panel|rooftop)", t):
                f = _factor(note)
                if f is not None:
                    d, adj = "solar_reduction", {"hours": hrs, "factor": f}
            elif re.search(r"(reserve|at least|minimum).*(kwh)|keep.*kwh", t):
                m = re.search(r"(\d+(?:\.\d+)?)\s*kwh", t)
                if m:
                    d, adj = "minimum_battery_reserve", {
                        "hours": hrs, "minimum_energy_kwh": float(m.group(1))}
            elif re.search(r"(no|not|don'?t|avoid|cannot|can't).{0,25}charg", t):
                d, adj = "no_charge_window", {"hours": hrs}
            elif re.search(r"(no|not|don'?t|avoid|cannot|can't).{0,25}(discharg|draw)", t):
                d, adj = "no_discharge_window", {"hours": hrs}
            elif re.search(r"(grid|import).{0,40}(cap|limit|not exceed|max)", t) or \
                 re.search(r"(cap|limit).{0,20}(grid|import)", t):
                m = re.search(r"(\d+(?:\.\d+)?)\s*kwh", t)
                if m:
                    d, adj = "max_grid_window", {"hours": hrs, "max_grid_kwh": float(m.group(1))}
        out.append({"note_index": i, "applies": d != "no_op", "directive_type": d,
                    "structured_adjustment": adj,
                    "explanation": "Interpreted by deterministic fallback (model unavailable)."})
    return out
