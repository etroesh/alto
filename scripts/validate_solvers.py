"""
validate_solvers.py - Prove the fast solver returns the optimal answer.

WHY THIS SCRIPT EXISTS
----------------------
The website runs the network flow solver because it answers in milliseconds.
That is only acceptable if the fast answer is also the RIGHT answer. This
script is the evidence.

It runs three independent methods on the same days and checks they agree:

  1. PEAK DEMAND - a sweep line counting the most aircraft on the ground at
     once. This is a mathematical lower bound: no assignment can use fewer
     gates than this. For interval-shaped problems like gate occupancy it is
     also the exact answer, so it is not just a bound, it is the truth.

  2. NETWORK FLOW - the production solver.

  3. INTEGER PROGRAM - the literal statement of the problem, solved exactly.

Three methods, three completely different pieces of mathematics. If all three
agree on every day tested, the fast solver is correct.

Run it with:   python scripts/validate_solvers.py
"""

import sys
import time

sys.path.insert(0, ".")

from alto import schedule, solver_ilp, solver_mcnf


# A spread across the year: quiet winter days, summer peaks, holidays, and the
# busiest day in the dataset. Cherry-picking easy days would prove nothing.
DAYS_TO_TEST = [
    "2023-01-04", "2023-01-15", "2023-02-11", "2023-03-08",
    "2023-04-19", "2023-05-27", "2023-06-14", "2023-07-04",
    "2023-07-15", "2023-07-23", "2023-08-11", "2023-08-25",
    "2023-09-16", "2023-10-21", "2023-11-22", "2023-12-24",
]


def validate_one_day(date_string, gates):
    """Run all three methods on one day and compare."""
    blocks = schedule.load_day(date_string)

    lower_bound = schedule.peak_demand(blocks)

    flow_start = time.time()
    flow_result = solver_mcnf.solve(blocks, gates)
    flow_seconds = time.time() - flow_start

    ilp_start = time.time()
    ilp_result = solver_ilp.solve(blocks, gates, time_limit_seconds=300)
    ilp_seconds = time.time() - ilp_start

    flow_gates = flow_result.get("gates_used")
    ilp_gates = ilp_result.get("gates_used")

    all_agree = (lower_bound == flow_gates == ilp_gates)

    return {
        "date": date_string,
        "blocks": len(blocks),
        "lower_bound": lower_bound,
        "flow_gates": flow_gates,
        "ilp_gates": ilp_gates,
        "agree": all_agree,
        "flow_seconds": flow_seconds,
        "ilp_seconds": ilp_seconds,
        "flow_walk": flow_result.get("total_walk_cost"),
        "ilp_walk": ilp_result.get("total_walk_cost"),
    }


def main():
    gates = schedule.load_gates()

    print("Validating the fast solver against the exact one")
    print("Gates available:", len(gates))
    print()
    header = (
        "date          blocks   bound   flow    ilp   agree   "
        "flow(s)   ilp(s)   speedup"
    )
    print(header)
    print("-" * len(header))

    results = []
    for date_string in DAYS_TO_TEST:
        result = validate_one_day(date_string, gates)
        results.append(result)

        speedup = result["ilp_seconds"] / max(result["flow_seconds"], 0.0001)
        print(
            "{date}   {blocks:>5}   {lower_bound:>5}   {flow_gates:>4}   "
            "{ilp_gates:>4}   {mark:^5}   {flow_seconds:>6.3f}   "
            "{ilp_seconds:>6.1f}   {speedup:>6.0f}x".format(
                mark="YES" if result["agree"] else "NO",
                speedup=speedup,
                **result
            )
        )

    print()
    days_agreeing = sum(1 for r in results if r["agree"])
    print("Agreement:", days_agreeing, "of", len(results), "days")

    if days_agreeing == len(results):
        print("RESULT: the network flow solver returned the proven optimal")
        print("        number of gates on every day tested.")
    else:
        print("RESULT: DISAGREEMENT FOUND. The flow formulation is wrong.")

    # The walking comparison is a separate, honest point.
    print()
    print("Walking cost, where the two solvers differ:")
    worse_count = 0
    total_gap = 0.0
    for r in results:
        if r["flow_walk"] is None or r["ilp_walk"] is None:
            continue
        gap = r["flow_walk"] - r["ilp_walk"]
        if gap > 0.01:
            worse_count += 1
            total_gap += gap / r["ilp_walk"] * 100
    if worse_count == 0:
        print("  identical on every day")
    else:
        print("  the flow solver is worse on", worse_count, "of",
              len(results), "days,")
        print("  by an average of", round(total_gap / worse_count, 1), "percent.")
        print()
        print("  This is expected and is not a bug. Network flow treats every")
        print("  gate as interchangeable, so it picks the gate NAMES in a")
        print("  second step after the chains are already fixed. The integer")
        print("  program chooses chains and gates together and can therefore")
        print("  find arrangements the flow solver cannot reach.")
        print("  Gate COUNT - the thing that drives cost and contract")
        print("  compliance - is optimal in both.")


if __name__ == "__main__":
    main()
