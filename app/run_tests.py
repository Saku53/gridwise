import json
import requests
import os
import math

API_URL = "http://localhost:8000/optimize-energy"

# Change this if your test file is somewhere else
TEST_FILE = "../Pasted text.txt"

TOL = 0.01


def close(a, b):
    return abs(float(a) - float(b)) <= TOL


def validate_case(case, result):
    errors = []

    expected = case["expected_output"]
    inp = case["input"]

    # --------------------------------------------------
    # 1. Scenario ID
    # --------------------------------------------------
    if result.get("scenario_id") != inp["scenario_id"]:
        errors.append(
            f"scenario_id mismatch: {result.get('scenario_id')}"
        )

    # --------------------------------------------------
    # 2. Directive interpretation
    # --------------------------------------------------
    actual_directives = result.get("directive_interpretation", [])
    expected_directives = expected["directive_interpretation"]

    if len(actual_directives) != len(expected_directives):
        errors.append(
            f"directive count: expected {len(expected_directives)}, "
            f"got {len(actual_directives)}"
        )
    else:
        for i, (actual, exp) in enumerate(
            zip(actual_directives, expected_directives)
        ):
            if actual.get("note_index") != exp["note_index"]:
                errors.append(
                    f"note {i}: wrong note_index"
                )

            if actual.get("applies") != exp["applies"]:
                errors.append(
                    f"note {i}: applies expected "
                    f"{exp['applies']}, got {actual.get('applies')}"
                )

            if actual.get("directive_type") != exp["directive_type"]:
                errors.append(
                    f"note {i}: directive_type expected "
                    f"{exp['directive_type']}, "
                    f"got {actual.get('directive_type')}"
                )

            # Check structured adjustment
            actual_adj = actual.get("structured_adjustment")
            expected_adj = exp.get("structured_adjustment")

            if expected_adj is None:
                if actual_adj is not None:
                    errors.append(
                        f"note {i}: expected structured_adjustment=None"
                    )
            else:
                if actual_adj is None:
                    errors.append(
                        f"note {i}: structured_adjustment is missing"
                    )
                else:
                    for key, value in expected_adj.items():
                        if actual_adj.get(key) != value:
                            errors.append(
                                f"note {i}: {key} expected "
                                f"{value}, got {actual_adj.get(key)}"
                            )

    # --------------------------------------------------
    # 3. Hourly plan
    # --------------------------------------------------
    plan = result.get("hourly_plan", [])

    if len(plan) != 24:
        errors.append(
            f"hourly_plan must contain 24 entries, got {len(plan)}"
        )
        return errors

    hours = [x.get("hour") for x in plan]

    if hours != list(range(24)):
        errors.append(
            f"hourly_plan hours are not exactly 0-23: {hours}"
        )

    # --------------------------------------------------
    # 4. Energy balance and battery checks
    # --------------------------------------------------
    battery = inp["battery"]

    capacity = battery["capacity_kwh"]
    initial = battery["initial_energy_kwh"]
    minimum = battery["minimum_energy_kwh"]
    max_charge = battery["max_charge_kwh_per_hour"]
    max_discharge = battery["max_discharge_kwh_per_hour"]

    previous_energy = initial

    for row, hour_data in zip(plan, inp["hours"]):
        h = row["hour"]

        grid = float(row.get("grid_kwh", 0))
        solar_used = float(row.get("solar_used_kwh", 0))
        battery_kwh = float(row.get("battery_kwh", 0))
        action = row.get("battery_action")
        energy_after = float(row.get("battery_energy_after_kwh", 0))

        demand = float(hour_data["demand_kwh"])
        solar = float(hour_data["solar_kwh"])

        # No negative grid
        if grid < -TOL:
            errors.append(f"hour {h}: negative grid_kwh")

        # Solar cannot exceed forecast
        if solar_used > solar + TOL:
            errors.append(
                f"hour {h}: solar_used exceeds forecast"
            )

        # Battery action
        if action == "charge":
            if battery_kwh < -TOL or battery_kwh > max_charge + TOL:
                errors.append(
                    f"hour {h}: invalid charge amount {battery_kwh}"
                )

            expected_energy = previous_energy + battery_kwh

        elif action == "discharge":
            if battery_kwh < -TOL or battery_kwh > max_discharge + TOL:
                errors.append(
                    f"hour {h}: invalid discharge amount {battery_kwh}"
                )

            expected_energy = previous_energy - battery_kwh

        elif action == "idle":
            if abs(battery_kwh) > TOL:
                errors.append(
                    f"hour {h}: idle action but battery_kwh={battery_kwh}"
                )

            expected_energy = previous_energy

        else:
            errors.append(
                f"hour {h}: invalid battery_action={action}"
            )
            expected_energy = previous_energy

        # Energy balance
        # grid + solar + discharge = demand + charge
        charge = battery_kwh if action == "charge" else 0
        discharge = battery_kwh if action == "discharge" else 0

        lhs = grid + solar_used + discharge
        rhs = demand + charge

        if not close(lhs, rhs):
            errors.append(
                f"hour {h}: energy balance failed "
                f"(lhs={lhs}, rhs={rhs})"
            )

        # Battery energy consistency
        if not close(energy_after, expected_energy):
            errors.append(
                f"hour {h}: battery energy mismatch "
                f"(expected {expected_energy}, got {energy_after})"
            )

        # Battery limits
        if energy_after < minimum - TOL:
            errors.append(
                f"hour {h}: battery below minimum "
                f"({energy_after} < {minimum})"
            )

        if energy_after > capacity + TOL:
            errors.append(
                f"hour {h}: battery exceeds capacity"
            )

        previous_energy = energy_after

    # --------------------------------------------------
    # 5. End-of-day neutrality
    # --------------------------------------------------
    final_energy = float(plan[-1]["battery_energy_after_kwh"])

    if not close(final_energy, initial):
        errors.append(
            f"end battery energy {final_energy} != initial {initial}"
        )

    # --------------------------------------------------
    # 6. Recalculate totals
    # --------------------------------------------------
    total_grid = sum(float(x["grid_kwh"]) for x in plan)

    calculated_cost = 0

    for row, hour_data in zip(plan, inp["hours"]):
        calculated_cost += (
            float(row["grid_kwh"])
            * float(hour_data["tariff_bdt_per_kwh"])
        )

    calculated_peak = max(
        float(x["grid_kwh"]) for x in plan
    )

    returned_grid = float(result.get("total_grid_kwh", 0))
    returned_cost = float(result.get("total_cost_bdt", 0))
    returned_peak = float(result.get("peak_grid_kwh", 0))

    if not close(returned_grid, total_grid):
        errors.append(
            f"total_grid_kwh incorrect: "
            f"returned {returned_grid}, calculated {total_grid}"
        )

    if not close(returned_cost, calculated_cost):
        errors.append(
            f"total_cost_bdt incorrect: "
            f"returned {returned_cost}, calculated {calculated_cost}"
        )

    if not close(returned_peak, calculated_peak):
        errors.append(
            f"peak_grid_kwh incorrect: "
            f"returned {returned_peak}, calculated {calculated_peak}"
        )

    # --------------------------------------------------
    # 7. Compare optimal cost with public reference
    # --------------------------------------------------
    expected_cost = float(expected["total_cost_bdt"])

    if not close(returned_cost, expected_cost):
        errors.append(
            f"cost is not optimal/reference cost: "
            f"expected {expected_cost}, got {returned_cost}"
        )

    return errors


def main():
    print("=" * 60)
    print("GridWise Public Test Suite")
    print("=" * 60)

    if not os.path.exists(TEST_FILE):
        print(f"\nERROR: Test file not found:")
        print(os.path.abspath(TEST_FILE))
        print("\nPut 'Pasted text.txt' in D:\\Downloads\\gridwise")
        return

    try:
        with open(TEST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"\nERROR reading test file: {e}")
        return

    cases = data.get("cases", [])

    print(f"\nFound {len(cases)} test cases.")
    print(f"API: {API_URL}\n")

    # Check server
    try:
        requests.get("http://localhost:8000", timeout=3)
    except requests.exceptions.RequestException:
        print("ERROR: Cannot connect to GridWise server.")
        print("Start it first with:")
        print("uvicorn app.main:app --port 8000")
        return

    passed = 0
    failed = 0

    for case in cases:
        case_id = case["id"]

        print("-" * 60)
        print(f"Testing {case_id}: {case.get('label', '')}")

        try:
            response = requests.post(
                API_URL,
                json=case["input"],
                timeout=120
            )

            if response.status_code != 200:
                print(f"FAIL: HTTP {response.status_code}")
                print(response.text[:1000])
                failed += 1
                continue

            result = response.json()

            errors = validate_case(case, result)

            if not errors:
                print("PASS")
                passed += 1
            else:
                print("FAIL")
                for error in errors:
                    print(f"  - {error}")
                failed += 1

        except requests.exceptions.Timeout:
            print("FAIL: Request timed out")
            failed += 1

        except Exception as e:
            print(f"FAIL: {type(e).__name__}: {e}")
            failed += 1

    print("\n" + "=" * 60)
    print("FINAL RESULT")
    print("=" * 60)
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")
    print(f"Total:  {len(cases)}")

    if failed == 0:
        print("\nALL TESTS PASSED")
    else:
        print("\nSome tests failed. Check the errors above.")


if __name__ == "__main__":
    main()