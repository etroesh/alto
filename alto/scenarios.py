"""
scenarios.py - Describing a disruption.

HOW THIS FILE FITS
------------------
    schedule.py  ->  [ THIS FILE ]  ->  costs.py      how much did it hurt
                                     ->  solver_*.py  what's the best recovery

A scenario is a description of something going wrong on a particular day.
This file does one job: take a normal day and produce the disrupted version
of it. It does not price anything and it does not re-optimize - those come
next.

Three kinds of disruption, which between them cover most of what actually
happens at a hub:

    DELAY      specific aircraft arrive late
    CLOSE      a gate, or a whole concourse, is unavailable
    REPRICE    the cost assumptions change

The third one sounds different from the other two but behaves the same way:
it changes the answer without changing the flight schedule, which is exactly
what a sensitivity slider on the website needs.
"""

from alto import config


# ===========================================================================
# BLOCK 1 - What a scenario is
# ===========================================================================
# A plain dictionary rather than a class, because this travels over the web as
# JSON. What the browser sends is literally this shape.
#
#   {
#     "date": "2023-07-15",
#     "delays": {"N434AS": 45, "N961AK": 20},   tail number -> minutes late
#     "closed_gates": ["C4", "C5"],
#     "closed_concourses": ["N"],
#     "cost_overrides": {"delay_cost_per_minute": 120.0}
#   }

def empty_scenario(date_string):
    """A scenario with nothing wrong - the baseline day."""
    return {
        "date": date_string,
        "delays": {},
        "closed_gates": [],
        "closed_concourses": [],
        "cost_overrides": {},
    }


# ===========================================================================
# BLOCK 2 - Applying delays to the schedule
# ===========================================================================

def apply_delays(blocks, delays):
    """Shift blocks later for the aircraft named in the scenario.

    delays maps a tail number to how many minutes late it is. Every block
    belonging to that aircraft moves later by that amount, because a plane
    that lands 45 minutes late needs its gate 45 minutes later too.

    Returns a NEW DataFrame. The original is left untouched so the "before"
    picture is still available for comparison - that is the whole point of a
    damage assessment.
    """
    disrupted = blocks.copy()

    shifted_starts = []
    shifted_ends = []
    applied = []

    for row in blocks.itertuples(index=False):
        minutes_late = delays.get(row.tail_number, 0)
        shifted_starts.append(row.start_minute + minutes_late)
        shifted_ends.append(row.end_minute + minutes_late)
        applied.append(minutes_late)

    disrupted["start_minute"] = shifted_starts
    disrupted["end_minute"] = shifted_ends
    disrupted["injected_delay"] = applied

    # Keep the untouched schedule alongside, so anything downstream can ask
    # "how far has this moved from where it was supposed to be?"
    disrupted["scheduled_start"] = list(blocks["start_minute"])
    disrupted["scheduled_end"] = list(blocks["end_minute"])

    return disrupted.sort_values("start_minute").reset_index(drop=True)


# ===========================================================================
# BLOCK 3 - Taking gates out of service
# ===========================================================================

def available_gates(gates, closed_gates=None, closed_concourses=None):
    """The gates still usable after the scenario's closures.

    Closing a whole concourse is the more interesting case. The North
    Satellite has 20 of Alaska's 57 gates and is only reachable by train, so
    losing it is both a big capacity hit and a realistic one - it has closed
    for construction in real life.
    """
    if closed_gates is None:
        closed_gates = []
    if closed_concourses is None:
        closed_concourses = []

    still_open = gates
    if len(closed_gates) > 0:
        still_open = still_open[~still_open["gate_id"].isin(closed_gates)]
    if len(closed_concourses) > 0:
        still_open = still_open[~still_open["concourse"].isin(closed_concourses)]

    return still_open.reset_index(drop=True)


# ===========================================================================
# BLOCK 4 - Changing the cost assumptions
# ===========================================================================

def resolve_costs(cost_overrides=None):
    """The cost figures to use, with any scenario overrides applied.

    Defaults come from config.py, where each one is sourced. The website's
    sliders send overrides. Returning a dictionary rather than reading
    config.py directly everywhere means a scenario can never accidentally
    change the defaults for the next request.
    """
    if cost_overrides is None:
        cost_overrides = {}

    costs = {
        "delay_cost_per_minute": config.DELAY_COST_PER_MINUTE,
        "gate_idle_cost_per_minute": config.GATE_IDLE_COST_PER_MINUTE,
        "remote_hardstand_fee": config.REMOTE_HARDSTAND_FEE,
        "delay_propagation_factor": config.DELAY_PROPAGATION_FACTOR,
    }
    costs.update(cost_overrides)
    return costs
