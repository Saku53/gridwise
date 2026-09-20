"""Post every case in a public samples JSON file to a running server and report
PASS/FAIL based on schema shape + constraint replay (not byte-exact match).

Usage: python scripts/run_samples.py public_samples.json http://localhost:8000
"""
import json
import sys

import httpx

DIRECTIVE_TYPES = {"solar_reduction", "minimum_battery_reserve", "no_charge_window",
                    "no_discharge_window", "max_grid_window", "no_op"}
TOL = 0.05


def replay_ok(resp: dict, req: dict) -> list[str]:
    errs = []
    hours = {h["hour"]: h for h in req["hours"]}
    battery = req["battery"]
    plan = {p["hour"]: p for p in resp.get("hourly_plan", [])}

    if sorted(plan) != list(range(24)):
        return ["hourly_plan missing or malformed hours"]

    e_prev = battery["initial_energy_kwh"]
    for h in range(24):
        p = plan[h]
        chg = p["battery_kwh"] if p["battery_action"] == "charge" else 0.0
        dis = p["battery_kwh"] if p["battery_action"] == "discharge" else 0.0
        lhs = p["grid_kwh"] + p["solar_used_kwh"] + dis
        rhs = hours[h]["demand_kwh"] + chg
        if abs(lhs - rhs) > TOL:
            errs.append(f"h{h} energy balance off by {abs(lhs-rhs):.3f}")
        e_after = e_prev + chg - dis
        if abs(e_after - p["battery_energy_after_kwh"]) > TOL:
            errs.append(f"h{h} battery state mismatch")
        if p["battery_energy_after_kwh"] > battery["capacity_kwh"] + TOL:
            errs.append(f"h{h} exceeds capacity")
        e_prev = p["battery_energy_after_kwh"]
    if abs(e_prev - battery["initial_energy_kwh"]) > TOL:
        errs.append("end-of-day neutrality violated")

    recompute_cost = sum(plan[h]["grid_kwh"] * hours[h]["tariff_bdt_per_kwh"] for h in range(24))
    if abs(recompute_cost - resp.get("total_cost_bdt", -1)) > 0.5:
        errs.append(f"total_cost_bdt mismatch: reported {resp.get('total_cost_bdt')} "
                    f"vs recomputed {recompute_cost:.2f}")
    return errs


def interp_ok(resp: dict, n_notes: int) -> list[str]:
    errs = []
    entries = resp.get("directive_interpretation", [])
    if [e.get("note_index") for e in entries] != list(range(n_notes)):
        errs.append("directive_interpretation not 0..N-1 in order")
    for e in entries:
        if e.get("directive_type") not in DIRECTIVE_TYPES:
            errs.append(f"unsupported directive_type {e.get('directive_type')}")
        if e.get("directive_type") == "no_op" and e.get("applies") is not False:
            errs.append("no_op must have applies=false")
        if e.get("directive_type") != "no_op" and e.get("applies") is not True:
            errs.append("non-no_op must have applies=true")
    return errs


def main():
    path, base_url = sys.argv[1], sys.argv[2].rstrip("/")
    cases = json.load(open(path))["cases"]

    r = httpx.get(f"{base_url}/health", timeout=10)
    print(f"/health -> {r.status_code} {r.text}")

    passed = 0
    for case in cases:
        req = case["input"]
        try:
            r = httpx.post(f"{base_url}/optimize-energy", json=req, timeout=35)
            resp = r.json()
            errs = []
            if r.status_code != 200:
                errs.append(f"HTTP {r.status_code}: {resp}")
            else:
                errs += interp_ok(resp, len(req["operator_notes"]))
                errs += replay_ok(resp, req)
        except Exception as exc:  # noqa: BLE001
            errs = [f"request failed: {exc}"]

        status = "PASS" if not errs else "FAIL"
        if status == "PASS":
            passed += 1
        print(f"[{status}] {case['id']}: {case.get('label','')}")
        for e in errs:
            print(f"    - {e}")

    print(f"\n{passed}/{len(cases)} cases passed")


if __name__ == "__main__":
    main()