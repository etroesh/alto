"""
precompute_baselines.py - Solve every day once and save a summary.

WHY
---
The website should show something the instant it loads, before any request to
the solver has finished. A year-at-a-glance chart - how busy each day was, how
many gates it needed, what it cost - is the natural thing to show, and it does
not change, so there is no reason to compute it on demand.

This script solves all 365 days and writes one small JSON file that ships with
the static site. The page draws the whole year immediately from that file, and
only calls the API when someone picks a day or changes a scenario. If the API
is cold or down, the site still loads and still has something to say.

Run it with:   python scripts/precompute_baselines.py
"""

import json
import sqlite3
import sys
import time

sys.path.insert(0, ".")

from alto import config, costs, scenarios, schedule, solver_mcnf


def every_date():
    """Every date present in the database, in order."""
    connection = sqlite3.connect(config.DATABASE_PATH)
    rows = connection.execute(
        "SELECT DISTINCT arrival_date FROM gate_blocks ORDER BY arrival_date"
    ).fetchall()
    connection.close()
    return [row[0] for row in rows]


def summarize(date_string, gates, cost_settings):
    """Solve one day and reduce it to the handful of numbers a chart needs."""
    blocks = schedule.load_day(date_string)
    solution = solver_mcnf.solve(blocks, gates)

    if not solution["feasible"]:
        return {
            "date": date_string,
            "blocks": len(blocks),
            "feasible": False,
            "gates_needed": solution.get("gates_needed"),
        }

    simulation = costs.simulate(blocks, solution["assignment"], cost_settings)
    priced = costs.price(blocks, solution["assignment"], simulation, cost_settings)

    turns = len(set(blocks["turn_id"]))
    gates_used = solution["gates_used"]

    return {
        "date": date_string,
        "turns": turns,
        "blocks": len(blocks),
        "aircraft": int(blocks["tail_number"].nunique()),
        "feasible": True,
        "gates_used": gates_used,
        "peak_demand": solution["minimum_possible_gates"],
        "idle_minutes": priced["idle_minutes"],
        "tows": priced["tows"],
        "common_use_turns": priced["common_use_turns"],
        "total_cost": priced["total_cost"],
        # Turns per gate per day, against the 6.0 that SLOA V requires of a
        # preferential-use gate. See docs/how-the-industry-works.md.
        "turns_per_gate": round(turns / gates_used, 2) if gates_used else 0,
    }


def main():
    gates = schedule.load_gates()
    cost_settings = scenarios.resolve_costs()
    dates = every_date()

    print("Solving", len(dates), "days...")
    started = time.time()

    summaries = []
    for index, date_string in enumerate(dates):
        summaries.append(summarize(date_string, gates, cost_settings))
        if (index + 1) % 50 == 0:
            print("  ", index + 1, "of", len(dates))

    elapsed = time.time() - started
    print("Done in", round(elapsed, 1), "seconds")

    output_path = config.PROJECT_ROOT / "website" / "data" / "baseline_index.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "generated_from": str(config.DATABASE_PATH.name),
        "year": config.YEAR,
        "carrier": config.CARRIER,
        "airport": config.AIRPORT,
        "gates_available": len(gates),
        "preferential_gates": config.PREFERENTIAL_GATE_COUNT,
        "common_use_gates": config.COMMON_USE_GATE_COUNT,
        "minimum_use_turns_per_day": config.MINIMUM_USE_TURNS_PER_DAY,
        "days": summaries,
    }

    with open(output_path, "w") as file_handle:
        json.dump(payload, file_handle, separators=(",", ":"))

    size_kb = output_path.stat().st_size / 1024
    print("Wrote", output_path.name, "-", round(size_kb, 1), "KB")

    workable = [s for s in summaries if s.get("feasible")]
    print()
    print("Across the year:")
    print("  days:", len(summaries), "| all feasible:", len(workable) == len(summaries))
    print("  gates used:  min", min(s["gates_used"] for s in workable),
          " median", sorted(s["gates_used"] for s in workable)[len(workable) // 2],
          " max", max(s["gates_used"] for s in workable))
    print("  turns/gate:  min", min(s["turns_per_gate"] for s in workable),
          " max", max(s["turns_per_gate"] for s in workable),
          " (SLOA V minimum is", config.MINIMUM_USE_TURNS_PER_DAY, ")")


if __name__ == "__main__":
    main()
