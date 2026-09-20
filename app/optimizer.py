"""Layer 3: deterministic optimizer.

Takes clean directives (never strings) and produces a valid, cost-minimal
24-hour plan. Three stages: compile directives into per-hour bounds, solve the
LP, then net/round/self-check so the emitted plan is exactly what the judge
replays.
"""
from __future__ import annotations

import logging

import pulp

log = logging.getLogger("gridwise.optimizer")
H = range(24)
TOL = 1e-6


def compile_directives(hours, battery, directives):
    """Directives -> per-hour numeric bounds. This is the only place they are read."""
    solar = [float(h.solar_kwh) for h in hours]
    min_res = [float(battery.minimum_energy_kwh)] * 24
    grid_cap: list[float | None] = [None] * 24
    no_chg = [False] * 24
    no_dis = [False] * 24

    for d in directives:
        t = d["directive_type"]
        for h in d["hours"]:
            if t == "solar_reduction":
                solar[h] *= d["factor"]            # multiplicative if several overlap
            elif t == "minimum_battery_reserve":
                min_res[h] = max(min_res[h], d["minimum_energy_kwh"])
            elif t == "max_grid_window":
                cap = d["max_grid_kwh"]
                grid_cap[h] = cap if grid_cap[h] is None else min(grid_cap[h], cap)
            elif t == "no_charge_window":
                no_chg[h] = True
            elif t == "no_discharge_window":
                no_dis[h] = True

    return {"solar": solar, "min_res": min_res, "grid_cap": grid_cap,
            "no_chg": no_chg, "no_dis": no_dis}


def _solve(hours, battery, b):
    cap = float(battery.capacity_kwh)
    e0 = float(battery.initial_energy_kwh)
    mc = float(battery.max_charge_kwh_per_hour)
    md = float(battery.max_discharge_kwh_per_hour)

    prob = pulp.LpProblem("gridwise", pulp.LpMinimize)
    g = [pulp.LpVariable(f"g{h}", lowBound=0,
                         upBound=b["grid_cap"][h]) for h in H]
    s = [pulp.LpVariable(f"s{h}", lowBound=0, upBound=b["solar"][h]) for h in H]
    c = [pulp.LpVariable(f"c{h}", lowBound=0, upBound=0 if b["no_chg"][h] else mc) for h in H]
    d = [pulp.LpVariable(f"d{h}", lowBound=0, upBound=0 if b["no_dis"][h] else md) for h in H]
    e = [pulp.LpVariable(f"e{h}", lowBound=b["min_res"][h], upBound=cap) for h in H]

    for h in H:
        prob += g[h] + s[h] + d[h] == float(hours[h].demand_kwh) + c[h]
        prob += e[h] == (e0 if h == 0 else e[h - 1]) + c[h] - d[h]
    prob += e[23] == e0  # end-of-day neutrality

    prob += (pulp.lpSum(g[h] * float(hours[h].tariff_bdt_per_kwh) for h in H)
             + 1e-6 * pulp.lpSum(c[h] + d[h] for h in H))  # tie-break: no idle cycling

    prob.solve(pulp.PULP_CBC_CMD(msg=0))
    if pulp.LpStatus[prob.status] != "Optimal":
        return None
    return ([v.value() or 0.0 for v in s], [(c[h].value() or 0.0) - (d[h].value() or 0.0) for h in H])


def _emit(hours, battery, b, solar_used, net):
    """Replay the netted decisions forward and emit the final plan."""
    cap, e0 = float(battery.capacity_kwh), float(battery.initial_energy_kwh)
    mc, md = float(battery.max_charge_kwh_per_hour), float(battery.max_discharge_kwh_per_hour)
    plan, e_prev = [], e0
    for h in H:
        n = net[h]
        n = min(n, mc, cap - e_prev)
        n = max(n, -md, b["min_res"][h] - e_prev)
        if b["no_chg"][h]:
            n = min(n, 0.0)
        if b["no_dis"][h]:
            n = max(n, 0.0)
        su = min(max(solar_used[h], 0.0), b["solar"][h])
        grid = float(hours[h].demand_kwh) + n - su
        if grid < 0:                       # curtail surplus solar instead of exporting
            su += grid
            grid = 0.0
        e_after = e_prev + n
        action = "charge" if n > TOL else "discharge" if n < -TOL else "idle"
        plan.append({
            "hour": h,
            "grid_kwh": round(max(grid, 0.0), 6),
            "solar_used_kwh": round(max(su, 0.0), 6),
            "battery_action": action,
            "battery_kwh": round(abs(n) if action != "idle" else 0.0, 6),
            "battery_energy_after_kwh": round(e_after, 6),
        })
        e_prev = e_after
    return plan


def self_check(plan, hours, battery, b) -> list[str]:
    """Re-verify every judge rule against the EMITTED numbers."""
    errs, e_prev = [], float(battery.initial_energy_kwh)
    for h in H:
        p = plan[h]
        chg = p["battery_kwh"] if p["battery_action"] == "charge" else 0.0
        dis = p["battery_kwh"] if p["battery_action"] == "discharge" else 0.0
        lhs = p["grid_kwh"] + p["solar_used_kwh"] + dis
        rhs = float(hours[h].demand_kwh) + chg
        if abs(lhs - rhs) > 0.005:
            errs.append(f"h{h} balance {lhs:.4f}!={rhs:.4f}")
        if p["solar_used_kwh"] > b["solar"][h] + 0.005:
            errs.append(f"h{h} solar over effective")
        if p["battery_action"] == "idle" and p["battery_kwh"] != 0:
            errs.append(f"h{h} idle with nonzero kwh")
        if chg > float(battery.max_charge_kwh_per_hour) + 0.005:
            errs.append(f"h{h} charge rate")
        if dis > float(battery.max_discharge_kwh_per_hour) + 0.005:
            errs.append(f"h{h} discharge rate")
        if b["no_chg"][h] and chg > 0.005:
            errs.append(f"h{h} charged in no_charge_window")
        if b["no_dis"][h] and dis > 0.005:
            errs.append(f"h{h} discharged in no_discharge_window")
        if b["grid_cap"][h] is not None and p["grid_kwh"] > b["grid_cap"][h] + 0.005:
            errs.append(f"h{h} grid cap")
        e_after = e_prev + chg - dis
        if abs(e_after - p["battery_energy_after_kwh"]) > 0.005:
            errs.append(f"h{h} battery state")
        if p["battery_energy_after_kwh"] < b["min_res"][h] - 0.005:
            errs.append(f"h{h} below reserve")
        if p["battery_energy_after_kwh"] > float(battery.capacity_kwh) + 0.005:
            errs.append(f"h{h} above capacity")
        e_prev = p["battery_energy_after_kwh"]
    if abs(e_prev - float(battery.initial_energy_kwh)) > 0.005:
        errs.append("end-of-day neutrality")
    return errs


def optimize(hours, battery, directives):
    """Returns (plan, totals, applied_directives). Never raises."""
    order = ["max_grid_window", "minimum_battery_reserve", "no_charge_window",
             "no_discharge_window"]
    active = list(directives)
    for stage in range(len(order) + 1):
        b = compile_directives(hours, battery, active)
        res = _solve(hours, battery, b)
        if res is not None:
            plan = _emit(hours, battery, b, *res)
            errs = self_check(plan, hours, battery, b)
            if errs:
                log.error("self-check failed: %s", errs[:5])
            return plan, _totals(plan, hours), active
        if stage < len(order):  # infeasible: shed the least-safe constraint class
            log.warning("infeasible; dropping %s", order[stage])
            active = [d for d in active if d["directive_type"] != order[stage]]

    b = compile_directives(hours, battery, [])   # last resort: battery idle all day
    plan = _emit(hours, battery, b, [min(b["solar"][h], float(hours[h].demand_kwh)) for h in H],
                 [0.0] * 24)
    return plan, _totals(plan, hours), []


def _totals(plan, hours):
    return {
        "total_grid_kwh": round(sum(p["grid_kwh"] for p in plan), 6),
        "total_cost_bdt": round(sum(p["grid_kwh"] * float(hours[p["hour"]].tariff_bdt_per_kwh)
                                    for p in plan), 6),
        "peak_grid_kwh": round(max(p["grid_kwh"] for p in plan), 6),
    }
