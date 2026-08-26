"""
costs.py - What a disruption costs, and how much of it you get back.

HOW THIS FILE FITS
------------------
    scenarios.py  ->  [ THIS FILE ]  ->  the two numbers the website exists to show
                      solver_*.py

This file answers the question the whole project is built around:

    Something went wrong this morning. What does it cost to do nothing,
    and how much of that can I recover by re-assigning gates?

Two numbers, side by side. Everything else is supporting detail.

THE IDEA BEHIND THE DAMAGE NUMBER
---------------------------------
The naive way to price a disruption is to add up the injected delay and
multiply by the cost per minute. That is wrong, and wrong in the direction
that makes the project look pointless, because it misses where the real money
goes.

When an aircraft lands 45 minutes late, it does not just cost 45 minutes. It
arrives at a gate that has already been promised to somebody else. The next
aircraft has to hold. Its passengers connect late. Its own departure slips,
which makes IT late into the next city, and late back at SEA for its evening
turn. One late arrival becomes six late departures.

So the damage calculation is a SIMULATION, not a multiplication. We keep the
original gate plan exactly as it was, push the delayed aircraft, and watch
the consequences ripple through the gates and down the aircraft rotations.
That ripple is the cost of doing nothing, and it is what re-optimizing
recovers.
"""

import math

from alto import config, schedule


# ===========================================================================
# BLOCK 1 - The simulation: what actually happens if nobody intervenes
# ===========================================================================
# Two forces push aircraft later, and they feed each other, which is why this
# has to loop rather than run once:
#
#   GATE CONTENTION   the gate you were promised is still occupied, so you wait
#   ROTATION CARRY    your aircraft arrived late, so your departure is late
#
# A gate hold makes a departure late, which makes that aircraft late for its
# next turn, which can make it miss its next gate slot. Each pass through the
# loop follows the delay one more step down the chain. We stop after
# MAX_PROPAGATION_DEPTH passes because delays damp out, and past four legs the
# effect is smaller than the error in our estimate of it.

def simulate(blocks, assignment, costs=None, max_depth=None):
    """Run the day forward with the gate plan held fixed.

    blocks      - a day's gate blocks, already shifted by any injected delay
    assignment  - block position -> gate id, the plan we are NOT changing
    Returns the final timing of every block, plus a record of why each one
    moved.
    """
    if costs is None:
        costs = {"delay_propagation_factor": config.DELAY_PROPAGATION_FACTOR}
    if max_depth is None:
        max_depth = config.MAX_PROPAGATION_DEPTH

    positions = list(range(len(blocks)))

    # Where each block WANTED to be, before anything went wrong.
    if "scheduled_start" in blocks.columns:
        scheduled_start = list(blocks["scheduled_start"])
        scheduled_end = list(blocks["scheduled_end"])
    else:
        scheduled_start = list(blocks["start_minute"])
        scheduled_end = list(blocks["end_minute"])

    # Where each block is now, which is what the loop below keeps adjusting.
    actual_start = list(blocks["start_minute"])
    actual_end = list(blocks["end_minute"])

    tails = list(blocks["tail_number"])

    # Why each block moved, so the website can explain itself rather than
    # just showing a number.
    reason = {}
    for position in positions:
        reason[position] = "on time"
        if actual_start[position] > scheduled_start[position]:
            reason[position] = "injected delay"

    # --- group blocks by gate and by aircraft, once ------------------------
    blocks_at_gate = {}
    for position in positions:
        gate = assignment.get(position)
        if gate is None:
            continue
        blocks_at_gate.setdefault(gate, []).append(position)

    blocks_on_tail = {}
    for position in positions:
        blocks_on_tail.setdefault(tails[position], []).append(position)
    for tail in blocks_on_tail:
        blocks_on_tail[tail].sort(key=lambda p: scheduled_start[p])

    # --- the loop ---------------------------------------------------------
    for _pass in range(max_depth):
        moved_something = False

        # FORCE 1: gate contention.
        for gate in blocks_at_gate:
            queue = sorted(blocks_at_gate[gate], key=lambda p: actual_start[p])
            for index in range(1, len(queue)):
                previous = queue[index - 1]
                current = queue[index]

                earliest_possible = actual_end[previous] + config.MIN_GATE_BUFFER_MINUTES
                if actual_start[current] < earliest_possible:
                    held = earliest_possible - actual_start[current]
                    actual_start[current] = actual_start[current] + held
                    actual_end[current] = actual_end[current] + held
                    reason[current] = "held for gate " + gate
                    moved_something = True

        # FORCE 2: the aircraft rotation.
        for tail in blocks_on_tail:
            chain = blocks_on_tail[tail]
            for index in range(1, len(chain)):
                previous = chain[index - 1]
                current = chain[index]

                minutes_late = actual_end[previous] - scheduled_end[previous]
                if minutes_late <= 0:
                    continue

                # Slack is the breathing room built into the schedule between
                # one block finishing and the next starting. A generous gap
                # soaks up lateness; a tight one passes almost all of it on.
                slack = scheduled_start[current] - scheduled_end[previous]
                unabsorbed = minutes_late - slack
                if unabsorbed <= 0:
                    continue

                carried = unabsorbed * costs["delay_propagation_factor"]
                would_start_at = scheduled_start[current] + carried

                if would_start_at > actual_start[current]:
                    shift = would_start_at - actual_start[current]
                    actual_start[current] = actual_start[current] + shift
                    actual_end[current] = actual_end[current] + shift
                    reason[current] = "aircraft arrived late"
                    moved_something = True

        if not moved_something:
            break

    return {
        "actual_start": actual_start,
        "actual_end": actual_end,
        "scheduled_start": scheduled_start,
        "scheduled_end": scheduled_end,
        "reason": reason,
    }


# ===========================================================================
# BLOCK 2 - Turning the simulation into dollars
# ===========================================================================

def parking_charge(minutes_away, costs):
    """What the airport charges for one spell of parking away from a gate.

    Port of Seattle tariff:
      up to 4 hours              $100 hardstand fee
      beyond that, per 12 hours  $200 for the first two periods
                                 $5,000 for every period after that
    """
    if minutes_away <= 0:
        return 0.0

    if minutes_away <= costs["remote_hardstand_hours"] * 60:
        return costs["remote_hardstand_fee"]

    periods = int(math.ceil(minutes_away / (12 * 60.0)))
    charge = costs["ron_fee_per_12_hours"] * min(periods, 2)
    if periods > 2:
        charge = charge + costs["ron_fee_beyond_24_hours"] * (periods - 2)
    return charge


def off_gate_minutes_for(blocks, arrival_position):
    """How long this aircraft is away, measured between its two gate blocks."""
    turn_id = blocks.iloc[arrival_position]["turn_id"]
    same_turn = blocks[blocks["turn_id"] == turn_id]
    departure_block = same_turn[same_turn["block_type"] == "departure"]
    if len(departure_block) == 0:
        return 0
    return int(departure_block.iloc[0]["start_minute"]
               - blocks.iloc[arrival_position]["end_minute"])


def price(blocks, assignment, simulation, costs=None):
    """Add up what this day costs, broken into parts you can argue about.

    Three components, each priced from a published figure:

      DELAY    minutes a departure slipped, times the airline's own cost per
               minute of block time
      IDLE     gate time paid for and not used
      TOWING   every long turn that needed a tug to a hardstand
    """
    if costs is None:
        from alto import scenarios
        costs = scenarios.resolve_costs()

    positions = list(range(len(blocks)))
    block_types = list(blocks["block_type"])

    actual_end = simulation["actual_end"]
    scheduled_end = simulation["scheduled_end"]

    # --- delay ------------------------------------------------------------
    # Only price departures. A block ending in a pushback is a flight leaving
    # late and that costs real money. A block ending because the aircraft got
    # towed away is not a departure and nobody is waiting on it.
    delay_minutes = 0.0
    delayed_departures = 0
    worst_delay = 0.0

    for position in positions:
        if block_types[position] == "arrival":
            continue                      # ends in a tow, not a departure
        slipped = actual_end[position] - scheduled_end[position]
        if slipped <= 0:
            continue
        delay_minutes = delay_minutes + slipped
        delayed_departures = delayed_departures + 1
        if slipped > worst_delay:
            worst_delay = slipped

    delay_cost = delay_minutes * costs["delay_cost_per_minute"]

    # --- idle gate time ---------------------------------------------------
    idle_minutes = 0.0
    blocks_at_gate = {}
    for position in positions:
        gate = assignment.get(position)
        if gate is None:
            continue
        blocks_at_gate.setdefault(gate, []).append(position)

    for gate in blocks_at_gate:
        queue = sorted(blocks_at_gate[gate], key=lambda p: simulation["actual_start"][p])
        for index in range(1, len(queue)):
            previous = queue[index - 1]
            current = queue[index]
            gap = (simulation["actual_start"][current]
                   - actual_end[previous]
                   - config.MIN_GATE_BUFFER_MINUTES)
            if gap > 0:
                idle_minutes = idle_minutes + gap

    idle_cost = idle_minutes * costs["gate_idle_cost_per_minute"]

    # --- parking away from a gate ------------------------------------------
    # Not a flat fee. The tariff charges $100 for a hardstand use of up to four
    # hours, and past that the Remain Overnight schedule takes over: $200 for
    # each of the first two 12-hour periods, then $5,000 for every period after
    # that. The last step is deliberately punishing - it is what stops carriers
    # using Sea-Tac as a car park - and an aircraft sitting for two days costs
    # far more than one sitting for six hours. A flat $100 hid all of that.
    tows = sum(1 for t in block_types if t == "arrival")
    towing_cost = 0.0
    for position in positions:
        if block_types[position] != "arrival":
            continue
        away = off_gate_minutes_for(blocks, position)
        towing_cost = towing_cost + parking_charge(away, costs)

    # --- common-use gates -------------------------------------------------
    # Alaska pays rent on its own gates, so one more turn on those costs
    # nothing at the margin. A common-use gate is billed per turn instead -
    # $552.89 for a narrowbody under the 2025 tariff. This is what makes
    # overflow visible as money rather than as a footnote.
    common_use_turns = 0
    for position in positions:
        gate = assignment.get(position)
        if gate is not None and gate.startswith("S"):
            common_use_turns = common_use_turns + 1
    common_use_cost = common_use_turns * costs["common_gate_turn_fee"]

    return {
        "delay_minutes": round(delay_minutes, 1),
        "delayed_departures": delayed_departures,
        "worst_single_delay": round(worst_delay, 1),
        "delay_cost": round(delay_cost, 2),
        "idle_minutes": round(idle_minutes, 1),
        "idle_cost": round(idle_cost, 2),
        "tows": tows,
        "towing_cost": round(towing_cost, 2),
        "common_use_turns": common_use_turns,
        "common_use_cost": round(common_use_cost, 2),
        "total_cost": round(delay_cost + idle_cost + towing_cost + common_use_cost, 2),
    }


# ===========================================================================
# BLOCK 3 - What "doing nothing" actually means when a gate closes
# ===========================================================================
# For a delay scenario, doing nothing is easy to define: keep the plan, let
# the aircraft arrive late, watch the queue back up.
#
# For a GATE CLOSURE it is not, because doing literally nothing is impossible.
# The aircraft assigned to a closed gate has to go somewhere. If we simply
# drop it from the plan, it stops costing anything at all - and the model
# reports that closing six gates SAVES money, which is obviously wrong. That
# was a real bug in the first version of this file.
#
# So the honest comparison is not "optimize versus nothing", it is
# "optimize versus what a gate controller does under pressure": take the
# displaced aircraft in time order and put each one at the first gate that is
# free. No lookahead, no re-planning, just the obvious greedy move.
#
# If no gate is free, the aircraft is sent to whichever one frees soonest and
# holds. We do not price that hold here - the simulation does, because a hold
# is exactly the gate contention it already knows how to follow.

def first_fit_for_displaced(blocks, kept_assignment, open_gates):
    """Place aircraft whose gate was closed, the way a human would.

    kept_assignment - blocks that still have their original, still-open gate
    Returns the full assignment, original placements plus the improvised ones.
    """
    assignment = dict(kept_assignment)

    gate_list = list(open_gates["gate_id"])
    if len(gate_list) == 0:
        # No gates at all. Nothing can be placed, and the caller must report
        # this rather than letting an empty roster propagate as a None gate.
        return assignment

    starts = list(blocks["start_minute"])
    ends = list(blocks["end_minute"])
    gate_ids = list(open_gates.sort_values("walk_cost")["gate_id"])

    # When each gate next becomes free, given what is already placed on it.
    occupied_until = {}
    for gate_id in gate_ids:
        occupied_until[gate_id] = []
    for position in assignment:
        occupied_until[assignment[position]].append(
            (starts[position], ends[position])
        )

    displaced = [p for p in range(len(blocks)) if p not in assignment]
    displaced.sort(key=lambda p: starts[p])

    for position in displaced:
        chosen_gate = None

        # First pass: a gate that is genuinely free for this whole window.
        for gate_id in gate_ids:
            clashes = False
            for busy_start, busy_end in occupied_until[gate_id]:
                overlaps = (starts[position] < busy_end + config.MIN_GATE_BUFFER_MINUTES
                            and busy_start < ends[position] + config.MIN_GATE_BUFFER_MINUTES)
                if overlaps:
                    clashes = True
                    break
            if not clashes:
                chosen_gate = gate_id
                break

        # Second pass: nothing is free, so join the shortest queue and hold.
        if chosen_gate is None:
            best_free_time = None
            for gate_id in gate_ids:
                if len(occupied_until[gate_id]) == 0:
                    frees_at = 0
                else:
                    frees_at = max(end for _, end in occupied_until[gate_id])
                if best_free_time is None or frees_at < best_free_time:
                    best_free_time = frees_at
                    chosen_gate = gate_id

        assignment[position] = chosen_gate
        occupied_until[chosen_gate].append((starts[position], ends[position]))

    return assignment


# ===========================================================================
# BLOCK 4 - Damage and recovery, side by side
# ===========================================================================
# This is the function the website's main screen calls. It runs the day three
# times and hands back all three so they can be compared:
#
#   BASELINE   the day as scheduled, optimally assigned
#   DAMAGE     the disruption, with the ORIGINAL gate plan stubbornly kept
#   RECOVERY   the disruption, with gates re-assigned around it
#
# The gap between damage and recovery is the value of the optimizer, stated
# in dollars. That single number is the product.

def damage_and_recovery(blocks, gates, scenario, solver, baseline_solution=None):
    """Price doing nothing against re-optimizing. Returns all three runs.

    baseline_solution lets the caller hand in an already-solved normal day.
    The baseline never changes for a given date and solver, so re-solving it on
    every scenario is pure waste - and with the integer program that waste was
    over twenty seconds per request.
    """
    from alto import scenarios

    costs = scenarios.resolve_costs(scenario.get("cost_overrides"))
    open_gates = scenarios.available_gates(
        gates, scenario.get("closed_gates"), scenario.get("closed_concourses")
    )

    # Someone will close every gate to see what happens. Catch it here rather
    # than letting an empty roster travel downstream and surface as a confusing
    # internal error - a scenario with nowhere to park is a legitimate question
    # with a clear answer.
    if len(open_gates) == 0:
        return {
            "feasible": False,
            "stage": "scenario",
            "reason": "Every gate is closed. There is nowhere to park any aircraft.",
        }

    # --- baseline: the day as it was meant to run -------------------------
    if baseline_solution is None:
        baseline_solution = solver.solve(blocks, gates)
    if not baseline_solution["feasible"]:
        return {"feasible": False, "stage": "baseline",
                "reason": baseline_solution.get("reason")}

    baseline_blocks = scenarios.apply_delays(blocks, {})
    baseline_simulation = simulate(baseline_blocks, baseline_solution["assignment"], costs)
    baseline_price = price(baseline_blocks, baseline_solution["assignment"],
                           baseline_simulation, costs)

    # --- damage: same plan, disrupted day ---------------------------------
    # The original assignment is keyed by position in the ORIGINAL block
    # order. apply_delays re-sorts by the new times, so positions move. We
    # follow block_id, which never changes, to carry the plan across.
    disrupted_blocks = scenarios.apply_delays(blocks, scenario.get("delays", {}))
    gate_by_block_id = {}
    for position in baseline_solution["assignment"]:
        block_id = blocks.loc[position, "block_id"]
        gate_by_block_id[block_id] = baseline_solution["assignment"][position]

    stubborn_assignment = {}
    for position in range(len(disrupted_blocks)):
        block_id = disrupted_blocks.loc[position, "block_id"]
        gate = gate_by_block_id.get(block_id)
        # If the scenario closed the gate this block was using, there is
        # nowhere for it to go under the original plan. That is exactly the
        # point: doing nothing is not an option, and the damage number should
        # say so.
        if gate is not None and gate in set(open_gates["gate_id"]):
            stubborn_assignment[position] = gate

    displaced_count = len(disrupted_blocks) - len(stubborn_assignment)

    # Anything whose gate was closed gets improvised onto another gate, the
    # way a controller would. Without this, closing gates appears to save
    # money because the displaced aircraft stop being counted.
    damage_assignment = first_fit_for_displaced(
        disrupted_blocks, stubborn_assignment, open_gates
    )

    damage_simulation = simulate(disrupted_blocks, damage_assignment, costs)
    damage_price = price(disrupted_blocks, damage_assignment,
                         damage_simulation, costs)

    # --- recovery: re-assign gates around the disruption -------------------
    # The recovery is told what the plan used to be, so among all the
    # arrangements that use the optimal number of gates it picks the one that
    # moves the fewest aircraft. Without this the solver returns an equally
    # good plan with the gates arbitrarily relabelled, and the site would
    # report hundreds of aircraft "moving" when nothing meaningful changed.
    recovery_solution = solver.solve(
        disrupted_blocks, open_gates, previous_assignment=stubborn_assignment
    )
    if not recovery_solution["feasible"]:
        return {
            "feasible": False,
            "stage": "recovery",
            "reason": recovery_solution.get("reason"),
            "baseline": baseline_price,
            "damage": damage_price,
        }

    recovery_simulation = simulate(disrupted_blocks, recovery_solution["assignment"], costs)
    recovery_price = price(disrupted_blocks, recovery_solution["assignment"],
                           recovery_simulation, costs)

    # --- what actually changed --------------------------------------------
    moved = []
    for position in recovery_solution["assignment"]:
        block_id = disrupted_blocks.loc[position, "block_id"]
        was = gate_by_block_id.get(block_id)
        now = recovery_solution["assignment"][position]
        if was is not None and was != now:
            moved.append({
                "block_id": int(block_id),
                "tail_number": disrupted_blocks.loc[position, "tail_number"],
                "from_gate": was,
                "to_gate": now,
            })

    recovered = damage_price["total_cost"] - recovery_price["total_cost"]
    disruption_cost = damage_price["total_cost"] - baseline_price["total_cost"]

    if disruption_cost > 0:
        recovered_share = round(recovered / disruption_cost * 100, 1)
    else:
        recovered_share = 0.0

    return {
        "feasible": True,
        "baseline": baseline_price,
        "damage": damage_price,
        "recovery": recovery_price,
        # The recovered plan itself, and the schedule it applies to. The web
        # layer needs these to redraw the chart. Returning them here means the
        # caller never has to solve the same day a second time just to find out
        # where the aircraft ended up - which, with the exact solver, was
        # doubling a twenty-second wait.
        "recovery_assignment": recovery_solution["assignment"],
        "disrupted_blocks": disrupted_blocks,
        "gate_before_by_block_id": gate_by_block_id,
        "gates_used_baseline": baseline_solution["gates_used"],
        "gates_used_recovery": recovery_solution["gates_used"],
        "blocks_displaced_by_closure": displaced_count,
        "aircraft_moved": moved,
        "aircraft_moved_count": len(moved),
        "disruption_cost": round(disruption_cost, 2),
        "recovered_dollars": round(recovered, 2),
        "recovered_percent": recovered_share,
    }
