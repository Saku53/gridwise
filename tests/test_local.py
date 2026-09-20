"""Local tests. Run: pytest tests/test_local.py
These do NOT call Gemini — they exercise guardrails + optimizer directly so
they run offline and fast. LLM interpretation itself is checked by
scripts/run_samples.py against a live server.
"""
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.guardrails import validate
from app.optimizer import compile_directives, optimize, self_check

ROWS = [(0,90,0,6),(1,85,0,6),(2,80,0,5),(3,80,0,5),(4,85,0,5),(5,95,0,6),
        (6,110,5,8),(7,130,20,10),(8,150,50,12),(9,165,90,14),(10,175,130,16),
        (11,180,160,16),(12,185,180,15),(13,180,170,14),(14,170,140,13),
        (15,165,90,14),(16,170,45,18),(17,185,10,22),(18,205,0,28),
        (19,215,0,30),(20,205,0,26),(21,175,0,18),(22,135,0,10),(23,105,0,7)]

def hours():
    return [NS(hour=h, demand_kwh=d, solar_kwh=s, tariff_bdt_per_kwh=t) for h, d, s, t in ROWS]

def battery():
    return NS(capacity_kwh=220, initial_energy_kwh=110, minimum_energy_kwh=40,
              max_charge_kwh_per_hour=50, max_discharge_kwh_per_hour=50)


def _assert_valid_plan(plan, hrs, bat, dirs):
    b = compile_directives(hrs, bat, dirs)
    errs = self_check(plan, hrs, bat, b)
    assert errs == [], errs
    assert len(plan) == 24
    assert sorted(p["hour"] for p in plan) == list(range(24))


def test_no_directives_baseline_feasible():
    hrs, bat = hours(), battery()
    plan, totals, applied = optimize(hrs, bat, [])
    _assert_valid_plan(plan, hrs, bat, applied)
    assert totals["total_cost_bdt"] > 0


def test_all_directive_types_together():
    hrs, bat = hours(), battery()
    dirs = [
        {"directive_type": "solar_reduction", "hours": [12, 13], "factor": 0.25},
        {"directive_type": "no_charge_window", "hours": [14, 15]},
        {"directive_type": "minimum_battery_reserve", "hours": [18, 19, 20],
         "minimum_energy_kwh": 120},
        {"directive_type": "max_grid_window", "hours": [9, 10, 11], "max_grid_kwh": 100},
    ]
    plan, totals, applied = optimize(hrs, bat, dirs)
    _assert_valid_plan(plan, hrs, bat, applied)
    for h in (14, 15):
        assert next(p for p in plan if p["hour"] == h)["battery_action"] != "charge"
    for h in (9, 10, 11):
        assert next(p for p in plan if p["hour"] == h)["grid_kwh"] <= 100 + 0.01
    for h in (18, 19, 20):
        assert next(p for p in plan if p["hour"] == h)["battery_energy_after_kwh"] >= 120 - 0.01


def test_guardrail_repairs_malformed_llm_output():
    bat = battery()
    raw = [
        {"note_index": 0, "applies": True, "directive_type": "solar_reduction",
         "structured_adjustment": {"hours": [13, 13, "14", 99], "factor": 20, "junk": 1},
         "explanation": "x"},
        {"note_index": 0, "applies": False, "directive_type": "no_op",
         "structured_adjustment": None, "explanation": "dup, should be dropped"},
        {"note_index": 5, "applies": True, "directive_type": "not_a_real_type",
         "structured_adjustment": {}, "explanation": "y"},
    ]
    entries, directives = validate(raw, 3, bat)
    assert len(entries) == 3
    assert [e["note_index"] for e in entries] == [0, 1, 2]
    e0 = entries[0]
    assert e0["directive_type"] == "solar_reduction"
    assert e0["structured_adjustment"]["hours"] == [13, 14]
    assert e0["structured_adjustment"]["factor"] == 0.2  # 20 parsed as 20% -> 0.2
    assert entries[1]["directive_type"] == "no_op" and entries[1]["applies"] is False
    assert entries[2]["directive_type"] == "no_op" and entries[2]["applies"] is False


def test_no_op_never_true_and_others_never_false():
    bat = battery()
    raw = [{"note_index": 0, "applies": False, "directive_type": "solar_reduction",
            "structured_adjustment": {"hours": [1], "factor": 0.5}, "explanation": "x"}]
    entries, _ = validate(raw, 1, bat)
    assert entries[0]["applies"] is True  # forced true for any non-no_op type


def test_infeasible_directive_combo_falls_back_gracefully():
    hrs, bat = hours(), battery()
    # deliberately extreme: cap grid at 0 for the whole day while forbidding both
    # charge and discharge everywhere -> optimizer must still return 24 valid hours
    dirs = [
        {"directive_type": "max_grid_window", "hours": list(range(24)), "max_grid_kwh": 0},
        {"directive_type": "no_charge_window", "hours": list(range(24))},
        {"directive_type": "no_discharge_window", "hours": list(range(24))},
    ]
    plan, totals, applied = optimize(hrs, bat, dirs)
    assert len(plan) == 24
    assert sorted(p["hour"] for p in plan) == list(range(24))
