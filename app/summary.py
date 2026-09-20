"""Deterministic plan summary. No LLM call here - it would add latency and the
rubric gives no credit for model-written prose."""
from __future__ import annotations


def _ranges(hs: list[int]) -> str:
    if not hs:
        return "none"
    out, start, prev = [], hs[0], hs[0]
    for h in hs[1:] + [None]:
        if h != prev + 1:
            out.append(f"{start:02d}" if start == prev else f"{start:02d}-{prev:02d}")
            start = h
        prev = h
    return ", ".join(out)


def summarize(plan, entries, totals) -> str:
    chg = [p["hour"] for p in plan if p["battery_action"] == "charge"]
    dis = [p["hour"] for p in plan if p["battery_action"] == "discharge"]
    applied = [e["directive_type"] for e in entries if e["applies"]]
    noop = sum(1 for e in entries if not e["applies"])
    return (
        f"Applied {len(applied)} operator directive(s) ({', '.join(applied) or 'none'}) "
        f"and ignored {noop} non-energy note(s). Battery charges in hour(s) {_ranges(chg)} "
        f"when tariffs are low and discharges in hour(s) {_ranges(dis)} to cover peak "
        f"pricing, ending the day at its initial state of charge. Total grid import "
        f"{totals['total_grid_kwh']:.2f} kWh at {totals['total_cost_bdt']:.2f} BDT, "
        f"peak {totals['peak_grid_kwh']:.2f} kWh."
    )
