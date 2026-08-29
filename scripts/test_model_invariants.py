"""Properties the model must satisfy, checked against real data.

    python3 scripts/test_model_invariants.py            # quick, 4 days
    python3 scripts/test_model_invariants.py --days 12  # slower, more days

WHY THIS FILE EXISTS
--------------------
Numbers in this project have changed more than once, and each version arrived
described as the correct one. That is a bad way to earn confidence in a model,
and it puts whoever is reading it in the position of having to trust a person
rather than check a result.

These are not unit tests of functions. They are statements about the WORLD that
any correct version of this model has to satisfy, whatever anybody's opinion of
the cost model happens to be this week:

    closing gates cannot make a day cheaper
    delaying an aircraft cannot make a day cheaper
    a bigger delay cannot cost less than a smaller one
    the plan cannot use fewer gates than the provable minimum
    the baseline cannot depend on the scenario being compared against it
    no cost component can be negative
    no field can be NaN

Every one of these was violated at some point by code that looked right. The
"closing gates cannot make a day cheaper" line is here because on 17 June with
N1, N11 and N20 closed, this model reported minus $682.

A FAILURE HERE IS INFORMATION, NOT AN EMBARRASSMENT. Print it, do not tune the
test until it passes.
"""

import sys
import argparse
import math
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from alto import costs, schedule, solver_mcnf, solver_ilp

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  PASS  " if ok else "  FAIL  ") + name + (("   " + detail) if detail else ""))
    return ok


def scenario(date, delays=None, closed=None):
    return {"date": date, "delays": delays or {}, "closed_gates": closed or [],
            "closed_concourses": [], "cost_overrides": {}}


def run(blocks, gates, date, delays=None, closed=None, solver=solver_mcnf, baseline=None):
    return costs.damage_and_recovery(
        blocks, gates, scenario(date, delays, closed), solver, baseline_solution=baseline
    )


def money_fields(priced):
    return {k: v for k, v in priced.items() if isinstance(v, (int, float))}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=4)
    parser.add_argument("--exact", action="store_true",
                        help="also check the integer program (slow)")
    args = parser.parse_args()

    gates = schedule.load_gates()
    every = ["2023-01-11", "2023-06-17", "2023-07-15", "2023-08-07",
             "2023-02-14", "2023-11-23", "2023-03-22", "2023-09-06",
             "2023-04-19", "2023-10-25", "2023-05-10", "2023-12-13"]
    dates = every[:args.days]

    for date in dates:
        print("\n" + date)
        blocks = schedule.load_day(date)
        base_solution = solver_mcnf.solve(blocks, gates)
        quiet = run(blocks, gates, date, baseline=base_solution)
        tails = [t for t in blocks["tail_number"].unique()[:3]]

        # 1. An undisturbed day costs nothing extra. Anything else means the
        #    comparison is not comparing like with like.
        check("undisturbed day has zero disruption cost",
              abs(quiet["disruption_cost"]) < 0.01,
              "got %.2f" % quiet["disruption_cost"])

        # 2. Closing gates can never make a day cheaper. This is the one that
        #    caught the priced-idle-time bug.
        for closed in (["C1", "C2", "C3"], ["N1", "N11", "N20"], ["C5"], ["S1", "S2"]):
            result = run(blocks, gates, date, closed=closed, baseline=base_solution)
            check("closing %-14s does not reduce cost" % ",".join(closed),
                  result["disruption_cost"] >= -0.01,
                  "disruption %.2f" % result["disruption_cost"])

        # 3. Delaying an aircraft can never make a day cheaper, and a bigger
        #    delay can never cost less than a smaller one.
        small = run(blocks, gates, date, delays={tails[0]: 30}, baseline=base_solution)
        large = run(blocks, gates, date, delays={tails[0]: 120}, baseline=base_solution)
        check("a 30-minute delay does not reduce cost",
              small["disruption_cost"] >= -0.01,
              "disruption %.2f" % small["disruption_cost"])
        check("120 minutes costs at least as much as 30",
              large["disruption_cost"] >= small["disruption_cost"] - 0.01,
              "%.2f vs %.2f" % (large["disruption_cost"], small["disruption_cost"]))

        # 4. The baseline is a property of the day, not of the scenario it is
        #    being compared against.
        closed_run = run(blocks, gates, date, closed=["C7", "C8"], baseline=base_solution)
        check("baseline is identical whatever the scenario",
              abs(closed_run["baseline"]["total_cost"] - quiet["baseline"]["total_cost"]) < 0.01,
              "%.2f vs %.2f" % (closed_run["baseline"]["total_cost"],
                                quiet["baseline"]["total_cost"]))

        # 5. No component may be negative, and nothing may be NaN. A NaN here
        #    is what took the API down once already.
        bad_negative, bad_nan = [], []
        for run_name in ("baseline", "damage", "recovery"):
            for field, value in money_fields(small[run_name]).items():
                if isinstance(value, float) and math.isnan(value):
                    bad_nan.append(run_name + "." + field)
                if "cost" in field and value < 0:
                    bad_negative.append("%s.%s=%.2f" % (run_name, field, value))
        check("no negative cost component", not bad_negative, ", ".join(bad_negative))
        check("no NaN anywhere in the priced output", not bad_nan, ", ".join(bad_nan))

        # 6. The plan cannot use fewer gates than the provable minimum. Gate
        #    occupancy is an interval graph, so peak demand IS the optimum -
        #    this is a theorem, not a preference.
        peak = schedule.peak_demand(blocks)
        used = base_solution["gates_used"]
        check("gates used equals the provable minimum",
              used == peak, "used %d, peak demand %d" % (used, peak))

        # 7. Re-optimising is allowed to help or do nothing. It is NOT allowed
        #    to be reported as recovering money it did not recover.
        check("recovered dollars never exceed the damage",
              small["recovered_dollars"] <= small["disruption_cost"] + 0.01,
              "recovered %.2f of %.2f" % (small["recovered_dollars"], small["disruption_cost"]))

        # 8. Re-planning must never be WORSE than doing nothing. The first
        #    version of this file checked only that recovery was not
        #    over-reported, which let a plan that cost MORE than the damage
        #    through - reported on screen as recovering minus $21,010.
        for closed in ([], ["C%d" % i for i in range(1, 28)]):
            result = run(blocks, gates, date, delays={tails[0]: 60},
                         closed=closed, baseline=base_solution)
            if not result.get("feasible", True):
                continue
            label = "with %d gates closed" % len(closed)
            check("re-planning is never worse than doing nothing " + label,
                  result["recovery"]["total_cost"] <= result["damage"]["total_cost"] + 0.01,
                  "recovery %.0f vs damage %.0f" % (result["recovery"]["total_cost"],
                                                    result["damage"]["total_cost"]))
            check("recovered dollars are never negative " + label,
                  result["recovered_dollars"] >= -0.01,
                  "recovered %.2f" % result["recovered_dollars"])

        # 8b. The severe-closure case, which is where this broke in public:
        #     39 gates shut leaves 18 leased stands, so most turns have to go
        #     to common-use. Reported on screen as recovering minus $38,011.
        severe = (["C%d" % i for i in range(1, 28)]
                  + ["D%d" % i for i in range(1, 11)] + ["N13", "N19"])
        result = run(blocks, gates, date, delays={tails[0]: 60},
                     closed=severe, baseline=base_solution)
        if result.get("feasible", True):
            check("severe closure: recovery is never worse than improvising",
                  result["recovery"]["total_cost"] <= result["damage"]["total_cost"] + 0.01,
                  "recovery %.0f vs damage %.0f" % (result["recovery"]["total_cost"],
                                                    result["damage"]["total_cost"]))
            check("severe closure: recovered dollars are never negative",
                  result["recovered_dollars"] >= -0.01,
                  "recovered %.2f" % result["recovered_dollars"])

        # 9. Closing MORE gates cannot make the OPTIMISED plan cheaper. Nested
        #    closures, so each scenario is strictly harder than the last.
        #
        #    THIS CHECK WAS DELIBERATELY NARROWED, AND HERE IS WHY.
        #    It first asked the same of the DAMAGE run and failed: on 15 July,
        #    damage across 0/3/10/17/27 closed gates went 82,816 / 85,793 /
        #    85,793 / 86,211 / 83,922 - up, then down at the end. The damage
        #    run is a greedy improvisation, and greedy is not monotone: with
        #    seventeen gates shut the survivors fragment the remaining
        #    capacity, while with all twenty-seven shut first-fit repacks the
        #    whole concourse cleanly. A real controller improvising under
        #    pressure is not monotone either.
        #
        #    Less capacity genuinely cannot produce a better plan, so the
        #    claim belongs on the plan the model actually computes. On the
        #    same day the recovery runs 49,209 / 49,209 / 49,209 / 49,209 /
        #    50,315 - never down. Narrowing a check to what is true is
        #    legitimate; widening a tolerance until a false claim passes is
        #    not, and that is not what happened here.
        ladder = []
        allC = ["C%d" % i for i in range(1, 28)]
        for size in (0, 3, 10, 17, 27):
            result = run(blocks, gates, date, delays={tails[0]: 60},
                         closed=allC[:size], baseline=base_solution)
            if result.get("feasible", True):
                ladder.append((size, result["recovery"]["total_cost"]))
        monotone = all(ladder[i][1] <= ladder[i + 1][1] + 0.01 for i in range(len(ladder) - 1))
        check("closing more gates never makes the optimised plan cheaper",
              monotone,
              " -> ".join("%d gates:%.0f" % (n, c) for n, c in ladder))

        if args.exact:
            exact_base = solver_ilp.solve(blocks, gates)
            check("integer program also matches the provable minimum",
                  exact_base["gates_used"] == peak,
                  "used %d, peak demand %d" % (exact_base["gates_used"], peak))
            exact_closed = run(blocks, gates, date, closed=["N1", "N11", "N20"],
                               solver=solver_ilp, baseline=exact_base)
            check("exact solver: closing gates does not reduce cost",
                  exact_closed["disruption_cost"] >= -0.01,
                  "disruption %.2f" % exact_closed["disruption_cost"])

    print("\n" + "=" * 64)
    print("%d passed, %d FAILED" % (len(PASS), len(FAIL)))
    for name in FAIL:
        print("   FAILED: " + name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
