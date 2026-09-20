"""Layer 2: deterministic guardrails.

Input:  untrusted entries from llm.py + the battery object.
Output: (response_entries, directives) where response_entries is exactly what the
        API returns and directives is a clean list the optimizer can trust.

Nothing here calls a model. Every repair is a fixed rule.
"""
from __future__ import annotations

import math
from typing import Any

ALLOWED = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}

DEFAULT_EXPLANATION = {
    "solar_reduction": "Usable solar is reduced during the stated hours.",
    "minimum_battery_reserve": "Battery energy must stay at or above the stated reserve.",
    "no_charge_window": "Battery charging is unavailable during the stated hours.",
    "no_discharge_window": "Battery discharging is unavailable during the stated hours.",
    "max_grid_window": "Grid import is capped during the stated hours.",
    "no_op": "This note does not affect today's 24-hour energy schedule.",
}


def _finite(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _clean_hours(raw: Any) -> list[int]:
    if not isinstance(raw, (list, tuple)):
        return []
    out = set()
    for h in raw:
        v = _finite(h)
        if v is None:
            continue
        iv = int(round(v))
        if 0 <= iv <= 23:
            out.add(iv)
    return sorted(out)


def _no_op(idx: int, why: str = "") -> dict[str, Any]:
    return {
        "note_index": idx,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": why or DEFAULT_EXPLANATION["no_op"],
    }


def validate(raw_entries: list[dict], n_notes: int, battery) -> tuple[list[dict], list[dict]]:
    by_index: dict[int, dict] = {}
    for e in raw_entries or []:
        if not isinstance(e, dict):
            continue
        idx = _finite(e.get("note_index"))
        if idx is None:
            continue
        idx = int(idx)
        if 0 <= idx < n_notes and idx not in by_index:  # first wins, drop duplicates
            by_index[idx] = e

    entries: list[dict] = []
    directives: list[dict] = []

    for i in range(n_notes):
        e = by_index.get(i)
        if e is None:
            entries.append(_no_op(i))  # missing mapping -> safe no_op
            continue

        dtype = e.get("directive_type")
        if dtype not in ALLOWED or dtype == "no_op":
            entries.append(_no_op(i, str(e.get("explanation") or "")[:300] or None))
            continue

        adj = e.get("structured_adjustment")
        if not isinstance(adj, dict):
            entries.append(_no_op(i))
            continue

        hours = _clean_hours(adj.get("hours"))
        if not hours:  # a windowed directive with no valid hours is meaningless
            entries.append(_no_op(i))
            continue

        clean: dict[str, Any] = {"hours": hours}

        if dtype == "solar_reduction":
            f = _finite(adj.get("factor"))
            if f is None:
                entries.append(_no_op(i))
                continue
            if 1.0 < f <= 100.0:  # model wrote a percentage
                f = f / 100.0
            clean["factor"] = min(1.0, max(0.0, f))

        elif dtype == "minimum_battery_reserve":
            r = _finite(adj.get("minimum_energy_kwh"))
            if r is None or r < 0:
                entries.append(_no_op(i))
                continue
            clean["minimum_energy_kwh"] = min(r, battery.capacity_kwh)

        elif dtype == "max_grid_window":
            g = _finite(adj.get("max_grid_kwh"))
            if g is None or g < 0:
                entries.append(_no_op(i))
                continue
            clean["max_grid_kwh"] = g

        explanation = str(e.get("explanation") or "").strip()[:300] or DEFAULT_EXPLANATION[dtype]
        entry = {
            "note_index": i,
            "applies": True,          # forced: only no_op may be false
            "directive_type": dtype,
            "structured_adjustment": clean,   # extra keys stripped by construction
            "explanation": explanation,
        }
        entries.append(entry)
        directives.append({"directive_type": dtype, **clean})

    return entries, directives
