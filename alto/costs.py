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

    # --- gate utilisation, which is NOT a cost --------------------------
    # IDLE GATE TIME USED TO BE PRICED HERE. IT NO LONGER IS, AND THAT IS THE
    # MOST IMPORTANT CORRECTION IN THIS FILE.
    #
    # What went wrong. Idle was the sum of the gaps between consecutive
    # aircraft at the gates in use, valued at a derived $6.14 a minute. On
    # 17 June, closing N1, N11 and N20 with the exact solver produced a
    # disruption cost of MINUS $682: closing gates appeared to SAVE money.
    #
    # The mechanism is simple once seen. Fewer gates in use means fewer
    # gate-days spanned, so fewer gaps to count. Neither solver minimises idle
    # anyway - the integer program minimises gate count then walking, the flow
    # solver minimises gate count then movement - so idle was never optimised,
    # only observed. An improvised first-fit after a closure could land on a
    # tighter packing than the "optimal" plan, and because idle was the largest
    # term ($105k of $146k that day) it swamped everything else.
    #
    # Rebasing it on leased capacity fixed the sign and produced a second
    # absurdity: $1.14 million, because the day's window was being stretched by
    # a single 36-hour turn.
    #
    # The real problem is that it should never have been a cost. This model's
    # stated rule is that every figure is a charge the airline actually pays.
    # Idle time is not billed. Alaska's rent on a preferential gate is fixed by
    # the lease and does not move when the plan changes, so putting it in a
    # COMPARISON of plans adds a large number that carries no information about
    # the decision - and, as above, actively misleads. The $6.14 was also the
    # only derived figure in the cost model, with no published source behind
    # it, which is exactly where a model earns an argument it cannot win.
    #
    # So utilisation is still measured and still reported, as the statistic it
    # always was. It is simply not added to a bill.
    occupied_minutes = 0.0
    for position in positions:
        gate = assignment.get(position)
        if gate is None:
            continue
        occupied_minutes = occupied_minutes + (
            actual_end[position] - simulation["actual_start"][position]
        )

    gates_in_use = len(set(
        assignment[position] for position in assignment if assignment[position]
    ))
    starts = [simulation["actual_start"][position] for position in positions]
    ends = [actual_end[position] for position in positions]
    window_minutes = (max(ends) - min(starts)) if positions else 0.0
    available_minutes = gates_in_use * window_minutes
    utilisation = (occupied_minutes / available_minutes * 100.0) if available_minutes else 0.0

    idle_minutes = max(available_minutes - occupied_minutes, 0.0)
    idle_cost = 0.0                     # kept at zero, never added to a total

    # --- aircraft with no gate at all ---------------------------------------
    # A block with no gate is one whose stand was closed in this scenario and
    # which nobody re-planned. It is not free and it is not invisible: it sits
    # on a remote hardstand and is billed for it.
    remote_parked = 0
    remote_parking_cost = 0.0
    for position in positions:
        if assignment.get(position) is not None:
            continue
        remote_parked = remote_parked + 1
        minutes = actual_end[position] - simulation["actual_start"][position]
        remote_parking_cost = remote_parking_cost + parking_charge(minutes, costs)

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
        "idle_cost": 0.0,               # retired - see the note above
        "gate_utilisation_percent": round(utilisation, 1),
        "gates_in_use": gates_in_use,
        "tows": tows,
        "towing_cost": round(towing_cost + remote_parking_cost, 2),
        "remote_parked": remote_parked,
        "remote_parking_cost": round(remote_parking_cost, 2),
        "common_use_turns": common_use_turns,
        "common_use_cost": round(common_use_cost, 2),
        # Delay, parking and common-use fees. Every one of them is a charge
        # somebody sends Alaska an invoice for.
        "total_cost": round(delay_cost + towing_cost + remote_parking_cost
                            + common_use_cost, 2),
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

    # WHERE A DISPLACED AIRCRAFT ACTUALLY GOES
    # ----------------------------------------
    # This used to hand every displaced aircraft to first_fit_for_displaced,
    # which found it another gate "the way a controller would". That was wrong
    # in a way that took a screenshot to notice: the greedy re-placement broke
    # the very queues that were propagating delay, so closing MORE gates made
    # a day CHEAPER. Measured on 15 July, closing 0/3/10/27 gates in Concourse
    # C: damage of $143,904 / $131,013 / $120,507 / $80,944. Monotonically
    # down. "Doing nothing" was quietly better than doing nothing.
    #
    # An aircraft whose gate is shut, with nobody re-planning, goes to a
    # remote stand. The tariff prices that - $100 for a hardstand use up to
    # four hours, then the Remain Overnight schedule - and price() charges it
    # for every block left without a gate. Nothing is silently dropped, and
    # the cost can only go up as more gates close.
    #
    # WHICH IS NOT THE SAME AS "STRAIGHT TO A HARDSTAND".
    #
    # Sending every displaced aircraft to a remote stand made "do nothing"
    # CHEAPER than re-planning, because a hardstand is $100 and a common-use
    # gate is $552.89 a turn. On 15 July with 39 gates closed: damage paid
    # $67,200 in parking and no common-use fees at all, while the re-plan paid
    # $48,654 in common-use fees - so re-planning was reported as recovering
    # minus $13,048. Five of ten days sampled did the same.
    #
    # The error is treating a hardstand as a substitute for a gate. It is not.
    # An aircraft on a remote stand at Sea-Tac cannot board passengers, so
    # parking there is not an option a controller can choose in order to save
    # money - it is what happens when there is no stand left at all.
    #
    # So a displaced aircraft takes whatever USABLE stand exists, cheapest
    # first, which is what first_fit_for_displaced does - leased gates before
    # common-use ones, because it walks the roster in walking-cost order and
    # the S stands sort last. It is billed for a common-use turn if it lands
    # on one. Only what genuinely will not fit anywhere goes to a hardstand,
    # and price() bills that separately.
    #
    # The delay artefact this used to cause - a greedy re-placement breaking
    # the queues that were propagating delay - is handled by the floor below,
    # not by refusing to place aircraft.
    damage_assignment = first_fit_for_displaced(
        disrupted_blocks, stubborn_assignment, open_gates
    )

    damage_simulation = simulate(disrupted_blocks, damage_assignment, costs)

    # THE FLOOR: closing a gate cannot make another aircraft leave EARLIER.
    #
    # Without this, taking aircraft off gates removes them from gate
    # contention, the simulated delay for the whole day falls, and a bigger
    # closure comes out cheaper than a smaller one. Measured on 15 July,
    # closing 0/3/10/27 gates in Concourse C gave damage totals of $82,816 /
    # $85,139 / $94,239 / $79,009 - rising correctly, then falling at the
    # largest closure.
    #
    # It is floored rather than modelled because the alternative needs a
    # number nobody publishes: an aircraft on a remote stand cannot board, so
    # its departure should slip, and by how much is not knowable from any
    # source this project has. What IS certain is the direction - a closure
    # never improves anyone's departure.
    #
    # The reference has to be the SAME DISRUPTED DAY WITH NOTHING CLOSED, not
    # the undisrupted baseline. Flooring against the baseline was tried first
    # and left a residual: closing three gates still came out $9 cheaper than
    # closing none, because the 53 aircraft it displaced left the gate queues
    # and their propagated delay fell back to the floor while the parking
    # fees they added did not quite cover it. Small, but the same wrong sign,
    # and a test that passes because a number is small is not a test.
    #
    # This costs one extra simulate and no solve: every block simply keeps
    # the gate the baseline gave it, closures ignored.
    unclosed_assignment = {}
    for position in range(len(disrupted_blocks)):
        block_id = disrupted_blocks.loc[position, "block_id"]
        gate = gate_by_block_id.get(block_id)
        if gate is not None:
            unclosed_assignment[position] = gate
    unclosed_simulation = simulate(disrupted_blocks, unclosed_assignment, costs)

    for position in range(len(disrupted_blocks)):
        floor = unclosed_simulation["actual_end"][position]
        if floor > damage_simulation["actual_end"][position]:
            damage_simulation["actual_end"][position] = floor
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

    # WHEN THE RE-PLAN IS WORSE THAN THE IMPROVISED PLAN
    # --------------------------------------------------
    # It happens, and it is not a rounding error. Under a severe closure the
    # FLOW solver can produce a plan that costs more than the controller's
    # improvisation, because it assigns whole CHAINS to gates and a chain
    # cannot be split. On 15 July with 39 gates closed - 18 leased gates left
    # against 31 chains - thirteen chains had to land on common-use stands,
    # carrying 83 turns at $552.89 each. First-fit places one block at a time
    # and got the same day onto 50 common-use turns. Measured, same day, same
    # closure, same delay:
    #
    #     flow solver   re-plan common-use $45,890   recovered  -$12,439
    #     exact solver  re-plan common-use $26,539   recovered   +$7,306
    #
    # The integer program has no chain constraint, so it does not have this
    # problem - this is the case where "solve exactly" earns its fifty seconds.
    #
    # An operator handed a worse plan would keep the one they had. So the
    # recovery is the BETTER of the two, and when the re-plan does not win,
    # that is reported as no improvement with a reason - not as a negative
    # recovery, and not by quietly hiding the comparison.
    recovery_improved = recovery_price["total_cost"] < damage_price["total_cost"] - 0.01
    if not recovery_improved:
        recovery_solution = {
            "feasible": True,
            "gates_used": damage_price["gates_in_use"],
            "assignment": damage_assignment,
            "minimum_possible_gates": recovery_solution.get("minimum_possible_gates"),
            "total_walk_cost": recovery_solution.get("total_walk_cost"),
        }
        recovery_simulation = damage_simulation
        recovery_price = damage_price

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

    recovered = max(damage_price["total_cost"] - recovery_price["total_cost"], 0.0)
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
        "recovery_improved": recovery_improved,
        "disruption_cost": round(disruption_cost, 2),
        "recovered_dollars": round(recovered, 2),
        "recovered_percent": recovered_share,
    }
