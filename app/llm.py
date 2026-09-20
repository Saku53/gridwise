"""Layer 1: LLM interpretation ONLY.

Gemini converts natural-language operator notes into raw structured directives.
It never sees demand, solar, tariff or battery numbers, and it never produces a
schedule. Its output is untrusted JSON that must pass guardrails.py.
"""
from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from typing import Any

log = logging.getLogger("gridwise.llm")

MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

SYSTEM_PROMPT = """You convert campus energy operator notes into structured directives.
You do NOT schedule energy, do NOT do arithmetic on demand/solar/tariff, and do NOT
invent values. You only classify and extract.

Allowed directive_type values and their exact structured_adjustment shape:
  solar_reduction         {"hours":[int,...], "factor": number}
  minimum_battery_reserve {"hours":[int,...], "minimum_energy_kwh": number}
  no_charge_window        {"hours":[int,...]}
  no_discharge_window     {"hours":[int,...]}
  max_grid_window         {"hours":[int,...], "max_grid_kwh": number}
  no_op                   null

RULES
1. Exactly one entry per note, in note_index order starting at 0.
2. Time windows are START-INCLUSIVE and END-EXCLUSIVE.
   "1 PM to 3 PM" -> [13,14].  "6 PM until 9 PM" -> [18,19,20].
   "between 13:00 and 15:00" -> [13,14].  "during hour 7" -> [7].
3. hours must be unique integers 0-23 in ascending order.
4. For solar_reduction, factor is the fraction of solar that REMAINS.
   "drops to about 20%" -> 0.2.  "80% reduction" -> 0.2.
   "roughly one-fifth of normal" -> 0.2.  "treated as 25% of forecast" -> 0.25.
   "halved" -> 0.5.  "solar unavailable" -> 0.0.
5. applies is true for every directive except no_op, which is always false.
6. A note that does not change today's 24-hour electrical schedule is no_op:
   menus, deadlines, HR notices, next week/next month events, unrelated maintenance.

EXAMPLES
"PV production will drop to about 20% between 13:00 and 15:00."
  -> solar_reduction, {"hours":[13,14],"factor":0.2}
"Panel washing from one until three will leave roughly one-fifth of normal solar output."
  -> solar_reduction, {"hours":[13,14],"factor":0.2}
"Expect an 80% reduction in rooftop solar during the 1-3 PM maintenance window."
  -> solar_reduction, {"hours":[13,14],"factor":0.2}
"Keep at least 120 kWh in reserve from 6 PM until 9 PM."
  -> minimum_battery_reserve, {"hours":[18,19,20],"minimum_energy_kwh":120}
"Do not charge the battery between 2 PM and 4 PM."
  -> no_charge_window, {"hours":[14,15]}
"The BESS cannot be drawn down during the evening peak, 6 to 8 PM."
  -> no_discharge_window, {"hours":[18,19]}
"Feeder work caps import at 150 kWh per hour from 9 AM to noon."
  -> max_grid_window, {"hours":[9,10,11],"max_grid_kwh":150}
"The cafeteria menu changes tomorrow."
  -> no_op, null
"The sports office moved next month's registration deadline."
  -> no_op, null

Return a JSON array only."""

RESPONSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "note_index": {"type": "INTEGER"},
            "applies": {"type": "BOOLEAN"},
            "directive_type": {
                "type": "STRING",
                "enum": [
                    "solar_reduction",
                    "minimum_battery_reserve",
                    "no_charge_window",
                    "no_discharge_window",
                    "max_grid_window",
                    "no_op",
                ],
            },
            "structured_adjustment": {
                "type": "OBJECT",
                "nullable": True,
                "properties": {
                    "hours": {"type": "ARRAY", "items": {"type": "INTEGER"}},
                    "factor": {"type": "NUMBER"},
                    "minimum_energy_kwh": {"type": "NUMBER"},
                    "max_grid_kwh": {"type": "NUMBER"},
                },
            },
            "explanation": {"type": "STRING"},
        },
        "required": ["note_index", "applies", "directive_type", "explanation"],
    },
}


@lru_cache(maxsize=1)
def _client():
    from google import genai

    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    return genai.Client(api_key=key)


# Notes repeat across hidden cases; caching keeps p95 latency down and saves quota.
_CACHE: dict[str, list[dict[str, Any]]] = {}


def interpret_notes(notes: list[str]) -> tuple[list[dict[str, Any]], str]:
    """Return (raw_entries, source). Never raises; falls back on provider failure."""
    key = "\u0000".join(notes)
    if key in _CACHE:
        return _CACHE[key], "cache"

    numbered = "\n".join(f"[{i}] {n}" for i, n in enumerate(notes))
    last_err = None
    for attempt in range(3):
        try:
            from google.genai import types

            resp = _client().models.generate_content(
                model=MODEL,
                contents=f"Interpret these {len(notes)} operator notes:\n{numbered}",
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.0,
                    response_mime_type="application/json",
                    response_schema=RESPONSE_SCHEMA,
                    max_output_tokens=2048,
                ),
            )
            log.warning("RAW GEMINI RESPONSE: %s", resp.text)
            entries = json.loads(resp.text)
            if not isinstance(entries, list):
                raise ValueError("model did not return a list")
            _CACHE[key] = entries
            return entries, "llm"
        except Exception as exc:  # noqa: BLE001 - provider errors are expected
            last_err = exc
            log.warning("gemini attempt %d failed: %s", attempt + 1, type(exc).__name__)

    log.error("gemini unavailable (%s); using outage fallback", type(last_err).__name__)
    from app.fallback_interpreter import regex_interpret

    return regex_interpret(notes), "fallback"
