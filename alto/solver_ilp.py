"""
solver_ilp.py - The reference solver. Integer linear programming.

HOW THIS FILE FITS
------------------
    schedule.py  ->  solver_mcnf.py   fast, used by the website
                 ->  [ THIS FILE ]    exact, used to prove the fast one right

This is the honest, literal statement of the problem: here are the blocks,
here are the gates, here are the rules, find the best assignment. It makes no
clever assumptions. That is exactly why it is slow, and exactly why it is the
one we trust.

It has two jobs:

  1. PROVE THE FAST SOLVER. Run both on the same day. If the network flow is
     really finding the optimum, the two must return the same number of gates.
     That check is what lets us put a millisecond solver on a public website
     and still say the answers are optimal.

  2. HANDLE WHAT FLOW CANNOT. Network flow needs every gate to be
     interchangeable. When gates stop being interchangeable - a flight that
     must be on Concourse C, a gate closed for maintenance, a stand that
     cannot take a 737 - the flow model has no way to say so. This one does.

THE MODEL
---------
Variables (the decisions the solver gets to make):
    x[b, g] = 1 if block b is parked at gate g, otherwise 0
    y[g]    = 1 if gate g is used at all that day, otherwise 0

Rules:
    1. Every block goes to exactly one gate.
    2. Blocks that are on the ground together cannot share a gate.
    3. If any block uses gate g, then gate g counts as used.

Goal:
    Use as few gates as possible. Among the plans that use the fewest gates,
    prefer the one with the least passenger walking.
"""

import pulp

from alto import config, schedule

# A NOTE ON SOMETHING THAT DID NOT WORK
# -------------------------------------
# It seemed obvious that handing the solver a shortlist of gates - the lower
# bound plus a little slack, instead of all 57 - would be faster. We know from
# the sweep line how many gates a day needs, and 57 interchangeable gates is a
# lot of symmetry to work through.
#
# It was measured and it was WORSE: 30.8 seconds against 21.7 for the full
# roster on 15 July, for an identical answer. Fewer variables, but a tighter
# problem is harder to prove optimal - the solver loses the slack that let it
# find and confirm a good incumbent quickly.
#
# The idea is left written down rather than silently dropped, because it is the
# kind of thing that looks obviously right and is worth not trying twice.


# ===========================================================================
# BLOCK 1 - Building the model
# ===========================================================================

def build_model(blocks, gates, closed_gates=None):
    """Write out the integer program. Returns the problem and its variables.

    closed_gates is an optional list of gate ids that are unavailable - the
    disruption scenario the website will use for "close a gate".
    """
    if closed_gates is None:
        closed_gates = []

    block_positions = list(range(len(blocks)))
    gate_ids = [g for g in gates["gate_id"] if g not in closed_gates]

    walk_cost = {}
    for row in gates.itertuples(index=False):
        walk_cost[row.gate_id] = row.walk_cost

    problem = pulp.LpProblem("gate_assignment", pulp.LpMinimize)

    # --- the decisions ----------------------------------------------------
    x = pulp.LpVariable.dicts(
        "x", (block_positions, gate_ids), cat="Binary"
    )
    y = pulp.LpVariable.dicts("y", gate_ids, cat="Binary")

    # --- the goal ---------------------------------------------------------
    # We want two things and one of them must always win. Rather than guessing
    # an exchange rate between "a gate" and "a unit of walking", we price a
    # gate at more than the total walking cost the day could possibly incur.
    # Then no amount of walking can ever justify an extra gate, and among
    # equal-gate plans the walking term decides. That is a lexicographic
    # objective, expressed as one sum.
    worst_possible_walk = len(blocks) * max(walk_cost.values())
    gate_price = worst_possible_walk + 1

    gates_term = pulp.lpSum(y[g] for g in gate_ids) * gate_price
    walk_term = pulp.lpSum(
        x[b][g] * walk_cost[g] for b in block_positions for g in gate_ids
    )
    problem += gates_term + walk_term

    # --- rule 1: every block gets exactly one gate ------------------------
    for b in block_positions:
        problem += pulp.lpSum(x[b][g] for g in gate_ids) == 1

    # --- rule 2: blocks on the ground together cannot share a gate --------
    # This uses the overlap GROUPS rather than pairs. Saying "at most one of
    # these 29 blocks may use gate C4" is one constraint that does the work of
    # 406 pairwise ones, and it is a tighter statement, so the solver reaches
    # the answer faster. On 15 July it is 125 groups instead of 4,398 pairs.
    groups = schedule.overlap_groups(blocks)
    for group in groups:
        for g in gate_ids:
            problem += pulp.lpSum(x[b][g] for b in group) <= y[g]

    # A block that never overlaps with anything appears in no group, so it
    # still needs its own link to y.
    blocks_in_groups = set()
    for group in groups:
        blocks_in_groups.update(group)
    for b in block_positions:
        if b in blocks_in_groups:
            continue
        for g in gate_ids:
            problem += x[b][g] <= y[g]

    # --- rule 3: break the symmetry ---------------------------------------
    # Every gate looks the same to the solver, so a plan using gates 1-29 and
    # a plan using gates 2-30 are identical in value. Without help the solver
    # wastes enormous effort proving that thousands of relabellings are all
    # equally good. Forcing gates to be opened in order collapses all of those
    # into one, and it is the single biggest speed-up in this file.
    for position in range(len(gate_ids) - 1):
        problem += y[gate_ids[position]] >= y[gate_ids[position + 1]]

    return problem, x, y, block_positions, gate_ids


# ===========================================================================
# BLOCK 2 - Solving it and reading the answer back
# ===========================================================================

# CBC gets 150 seconds, not 300.
#
# nginx gives the API 180 seconds before returning a gateway timeout, so a
# solver allowed to run for 300 guarantees that the slowest days come back to
# the visitor as an error page rather than as an answer. That happened in
# production. The limit here has to sit BELOW the proxy's, with room for the
# rest of the request.
DEFAULT_TIME_LIMIT_SECONDS = 150


def solve(blocks, gates, closed_gates=None, time_limit_seconds=DEFAULT_TIME_LIMIT_SECONDS,
          previous_assignment=None):
    """Solve the integer program. Returns a result shaped like the MCNF one,
    so the two can be compared directly and the API can use either."""
    if len(blocks) == 0:
        return {"feasible": True, "gates_used": 0, "assignment": {},
                "total_walk_cost": 0.0, "proven_optimal": True}

    # previous_assignment is accepted so this solver is a drop-in replacement
    # for the network flow one. The integer program does not use it yet -
    # minimizing aircraft movement would be a third term in the objective, and
    # it is listed as future work rather than half-implemented here.
    minimum_possible_gates = schedule.peak_demand(blocks)

    problem, x, y, block_positions, gate_ids = build_model(
        blocks, gates, closed_gates
    )

    solver = pulp.PULP_CBC_CMD(msg=False, timeLimit=time_limit_seconds)
    problem.solve(solver)

    status = pulp.LpStatus[problem.status]
    if status not in ("Optimal",):
        return {
            "feasible": False,
            "reason": "Solver returned status: " + status,
            "gates_needed": minimum_possible_gates,
        }

    # Read the chosen assignment back out of the variables.
    assignment = {}
    for b in block_positions:
        for g in gate_ids:
            if pulp.value(x[b][g]) > 0.5:      # binaries come back as 0.999...
                assignment[b] = g
                break

    gates_used = sum(1 for g in gate_ids if pulp.value(y[g]) > 0.5)

    # "proven_optimal" used to be hard-coded True, which is a claim this
    # function is not always entitled to make: with a time limit set, CBC can
    # stop early and still report a status of Optimal.
    #
    # But there IS a proof available here, and it does not depend on the
    # solver. Gate occupancy is an interval graph, so its chromatic number
    # equals its clique number - the sweep-line peak demand is exactly the
    # minimum number of gates any valid plan can use. If the solver matched it,
    # the gate count is provably optimal no matter how long CBC ran. Walking
    # cost has no such proof, so the claim is now scoped to what is provable.
    gate_count_proven_optimal = (gates_used == minimum_possible_gates)

    walk_cost = {}
    for row in gates.itertuples(index=False):
        walk_cost[row.gate_id] = row.walk_cost
    total_walk = sum(walk_cost[assignment[b]] for b in assignment)

    return {
        "feasible": True,
        "gates_used": gates_used,
        "minimum_possible_gates": minimum_possible_gates,
        "assignment": assignment,
        "total_walk_cost": round(total_walk, 2),
        "proven_optimal": gate_count_proven_optimal,
        "time_limit_seconds": time_limit_seconds,
        "variables": len(block_positions) * len(gate_ids) + len(gate_ids),
        "constraints": len(problem.constraints),
    }
