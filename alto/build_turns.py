"""
build_turns.py - Turn one year of BTS flight records into aircraft ground visits.

WHY THIS FILE EXISTS, AND WHAT IT REPLACES
------------------------------------------
The first version of this project built turns with a pandas merge:

    turns = pd.merge(arrivals, departures,
                     on=['Tail_Number', 'Month', 'DayofMonth'])

That is a cartesian join, and it is wrong. If tail N123AS arrives at SEA twice
in one day and departs twice, the merge produces 2 x 2 = 4 rows instead of 2.
Filtering with ArrTime <= DepTime removes some of the bad rows but keeps the
worst kind: the 07:00 arrival paired with the 21:00 departure from a
completely different visit.

The damage was not cosmetic. It inflated the 2023 dataset from 59,470 real
turns to 79,052, pushed the 75th-percentile turn time from 106 minutes to 305
minutes, and drove peak simultaneous gate occupancy on 15 July from 29 to 76.
Because Alaska only has 57 gates, that made the gate assignment model
INFEASIBLE - not slow, infeasible. No assignment satisfying the constraints
exists when 76 aircraft need a gate at once and 57 gates exist.

The fix is to stop treating a day as a bag of arrivals and a bag of departures,
and instead walk each aircraft's timeline in order: every arrival is paired
with the NEXT departure that same tail makes from SEA, and each departure can
only be used once.

This also fixes two things the merge could never have handled:
  1. Overnight parking (arrive 23:40, depart 06:15 the next morning). The old
     join was same-day only, so every one of those turns vanished.
  2. Red-eye arrivals whose clock time is earlier than their departure time
     because the flight crossed midnight.
"""

import heapq

import pandas as pd

from alto import config


# ---------------------------------------------------------------------------
# Time handling
# ---------------------------------------------------------------------------
# BTS stores clock times as integers in HHMM form: 1435 means 14:35, 605 means
# 06:05. We convert everything to "absolute minutes" - minutes elapsed since
# midnight on 1 January of the data year - so that comparing two timestamps is
# just comparing two integers, and an overnight turn is no different from any
# other turn.

MINUTES_PER_DAY = 24 * 60


def nan_to_zero(value):
    """Return 0.0 for a missing value, otherwise the value as a float."""
    if pd.isna(value):
        return 0.0
    return float(value)


def clock_to_minutes_of_day(hhmm):
    """Convert a BTS HHMM integer to minutes after midnight.

    Returns a tuple: (minutes_after_midnight, day_rollover)

    day_rollover is 1 when the value was 2400, which BTS uses for midnight at
    the END of a day. 2400 means 00:00 on the following day, so we return 0
    minutes and a rollover of 1 rather than the nonsensical 1440.
    """
    hours = int(hhmm) // 100
    minutes = int(hhmm) % 100

    if hours == 24:
        return 0, 1

    return hours * 60 + minutes, 0


def build_absolute_minutes(flight_date, hhmm, extra_days):
    """Minutes since midnight on 1 Jan of the data year.

    flight_date  - a pandas Timestamp for the flight's scheduled date
    hhmm         - the BTS clock time integer
    extra_days   - how many days past flight_date this event actually happened
    """
    minute_of_day, rollover = clock_to_minutes_of_day(hhmm)
    day_of_year_index = flight_date.dayofyear - 1        # 1 Jan -> 0
    total_days = day_of_year_index + extra_days + rollover
    return total_days * MINUTES_PER_DAY + minute_of_day


# ---------------------------------------------------------------------------
# Step 1: load and clean the flight records
# ---------------------------------------------------------------------------

def load_flights(csv_path=None):
    """Read the filtered AS/SEA flight file and drop rows we cannot use."""
    if csv_path is None:
        csv_path = config.FILTERED_FLIGHTS_CSV

    columns_we_need = [
        "FlightDate",
        "Tail_Number",
        "Flight_Number_Reporting_Airline",
        "Origin",
        "Dest",
        "CRSDepTime", "DepTime", "DepDelayMinutes",
        "CRSArrTime", "ArrTime", "ArrDelayMinutes",
        "Cancelled", "Diverted",
    ]

    flights = pd.read_csv(csv_path, usecols=columns_we_need, low_memory=False)
    flights["FlightDate"] = pd.to_datetime(flights["FlightDate"])

    # A cancelled flight never happened and a diverted flight did not land
    # where the schedule said it would, so neither one occupies a gate at SEA.
    flights = flights[flights["Cancelled"] == 0]
    flights = flights[flights["Diverted"] == 0]

    # Without a tail number we cannot connect an arrival to a departure at all.
    flights = flights[flights["Tail_Number"].notna()]

    return flights


# ---------------------------------------------------------------------------
# Step 2: split into arrival events and departure events at SEA
# ---------------------------------------------------------------------------

def build_arrival_events(flights):
    """Every flight that LANDED at SEA, with an absolute arrival time.

    The subtle part is the midnight crossing. BTS stamps a flight with the date
    it DEPARTED. A red-eye that leaves Anchorage at 23:30 and lands at SEA at
    04:10 is recorded with ArrTime = 410 on the departure date, even though it
    actually touched down the next calendar day. We detect that by comparing
    the arrival clock time to the departure clock time: if the plane "arrived"
    earlier in the day than it left, it crossed midnight.
    """
    arrivals = flights[flights["Dest"] == config.AIRPORT].copy()
    arrivals = arrivals[arrivals["ArrTime"].notna()]
    arrivals = arrivals[arrivals["DepTime"].notna()]

    absolute_times = []
    for row in arrivals.itertuples(index=False):
        arrival_clock, _ = clock_to_minutes_of_day(row.ArrTime)
        departure_clock, _ = clock_to_minutes_of_day(row.DepTime)

        if arrival_clock < departure_clock:
            days_past_flight_date = 1     # crossed midnight
        else:
            days_past_flight_date = 0

        absolute_times.append(
            build_absolute_minutes(row.FlightDate, row.ArrTime, days_past_flight_date)
        )

    arrivals["event_minute"] = absolute_times
    return arrivals


def build_departure_events(flights):
    """Every flight that TOOK OFF from SEA, with an absolute departure time.

    Departures are simpler than arrivals: BTS stamps the flight with its
    departure date, and the departure happens on that date by definition, so
    there is no midnight crossing to correct for.
    """
    departures = flights[flights["Origin"] == config.AIRPORT].copy()
    departures = departures[departures["DepTime"].notna()]

    absolute_times = []
    for row in departures.itertuples(index=False):
        absolute_times.append(
            build_absolute_minutes(row.FlightDate, row.DepTime, 0)
        )

    departures["event_minute"] = absolute_times
    return departures


# ---------------------------------------------------------------------------
# Step 3: pair each arrival with the next departure by the same aircraft
# ---------------------------------------------------------------------------

def pair_turns(arrivals, departures):
    """Walk each tail number's timeline and build its ground visits.

    The rule, stated plainly: an aircraft that lands at SEA stays on the ground
    until the next time that same aircraft takes off from SEA. Each departure
    can be consumed by exactly one arrival, which is what stops the cartesian
    blowup - once a departure has been claimed, it is gone.

    This is written as an explicit loop rather than a merge because the logic
    IS sequential. Trying to express "the next unclaimed departure" as a join
    condition is what caused the original bug.
    """
    # Group both sides by tail number once, so the loop below is a dictionary
    # lookup instead of a repeated scan over the whole DataFrame.
    arrivals_by_tail = {}
    for tail, group in arrivals.groupby("Tail_Number"):
        arrivals_by_tail[tail] = group.sort_values("event_minute")

    departures_by_tail = {}
    for tail, group in departures.groupby("Tail_Number"):
        departures_by_tail[tail] = group.sort_values("event_minute")

    turns = []
    dropped_too_short = 0
    dropped_too_long = 0
    dropped_no_departure = 0

    for tail in arrivals_by_tail:
        tail_arrivals = arrivals_by_tail[tail]

        if tail not in departures_by_tail:
            # This aircraft flew into SEA during 2023 and, as far as our data
            # shows, never flew out. Usually it left on a carrier code we
            # filtered out, or the year simply ended.
            dropped_no_departure += len(tail_arrivals)
            continue

        tail_departures = departures_by_tail[tail]

        # A plain list of departure times we can pop from as they get used.
        # Sorted ascending, so index 0 is always the earliest unclaimed one.
        unclaimed_departures = list(tail_departures.itertuples(index=False))

        for arrival in tail_arrivals.itertuples(index=False):
            # Find the first unclaimed departure that happens after this
            # arrival. Everything before it is a departure we somehow missed
            # the matching arrival for, so we discard those as we walk past.
            matched_departure = None
            while len(unclaimed_departures) > 0:
                candidate = unclaimed_departures[0]
                if candidate.event_minute > arrival.event_minute:
                    matched_departure = candidate
                    unclaimed_departures.pop(0)
                    break
                unclaimed_departures.pop(0)

            if matched_departure is None:
                dropped_no_departure += 1
                continue

            ground_minutes = matched_departure.event_minute - arrival.event_minute

            if ground_minutes < config.MIN_TURN_MINUTES:
                dropped_too_short += 1
                continue

            if ground_minutes > config.MAX_TURN_MINUTES:
                # The gap is too big to be one ground visit. We do NOT pair
                # across it, and we put the departure back so a later arrival
                # can claim it.
                unclaimed_departures.insert(0, matched_departure)
                dropped_too_long += 1
                continue

            turns.append({
                "tail_number": tail,
                "arrival_flight": int(arrival.Flight_Number_Reporting_Airline),
                "departure_flight": int(matched_departure.Flight_Number_Reporting_Airline),
                "arrival_origin": arrival.Origin,
                "departure_dest": matched_departure.Dest,
                "arrival_date": arrival.FlightDate.date().isoformat(),
                "arrival_minute": int(arrival.event_minute),
                "departure_minute": int(matched_departure.event_minute),
                "ground_minutes": int(ground_minutes),
                # BTS leaves the delay columns blank for on-time flights, and
                # a blank reads as NaN. NaN is "truthy" in Python, so the usual
                # "value or 0.0" idiom would silently keep the NaN. We test for
                # it explicitly instead.
                "arrival_delay_minutes": nan_to_zero(arrival.ArrDelayMinutes),
                "departure_delay_minutes": nan_to_zero(matched_departure.DepDelayMinutes),
                "scheduled_arrival_minute": int(build_absolute_minutes(
                    arrival.FlightDate, arrival.CRSArrTime, 0)),
                "scheduled_departure_minute": int(build_absolute_minutes(
                    matched_departure.FlightDate, matched_departure.CRSDepTime, 0)),
            })

    report = {
        "turns_built": len(turns),
        "dropped_too_short": dropped_too_short,
        "dropped_too_long": dropped_too_long,
        "dropped_no_departure": dropped_no_departure,
    }
    return pd.DataFrame(turns), report


# ---------------------------------------------------------------------------
# Step 4: convert turns into the gate occupancies the optimizer actually sees
# ---------------------------------------------------------------------------

def occupancy_intervals(turns, gate_count=None):
    """Work out which aircraft actually need a gate, and when.

    WHY THIS IS NOT A SIMPLE TIME LIMIT
    -----------------------------------
    The first version of this function towed any aircraft whose ground time
    exceeded three hours. That was wrong, and measuring it showed exactly how
    wrong. At the busiest moment of 2023 it produced:

        19 aircraft at gates   ...   57 aircraft on remote hardstands

    which is backwards. It emptied the gates overnight and queued everything on
    stands, because it decided who to tow from the clock alone without ever
    asking whether the gate was wanted by anybody.

    Real ramps do not work on a timer. An aircraft sits at its gate until
    somebody else needs it. At three in the morning nobody does, so it stays.
    At the morning push everybody does, so the aircraft with the longest wait
    ahead of it gets moved.

    So this walks the day in order and tows only under pressure:

        every arrival takes a gate if one is free
        if none is free, the aircraft already at a gate with the LONGEST wait
            still ahead of it is towed to a hardstand, and comes back an hour
            before its own departure to board
        if nobody can be moved - everyone is still deplaning or already
            boarding - the arriving aircraft HOLDS, which is what really
            happens, and the hold is recorded as a cost

    The difference is not subtle. Across 2023 this tows 547 aircraft instead of
    22,030, and peak hardstand use falls from 72 to 19 - which is exactly the
    overflow you would expect, since 76 aircraft are on the ground at the
    year's peak and there are 57 gates.
    """
    if gate_count is None:
        gate_count = config.GATE_COUNT

    buffer_minutes = config.MIN_GATE_BUFFER_MINUTES

    arrival_of = list(turns["arrival_minute"])
    departure_of = list(turns["departure_minute"])
    count = len(arrival_of)

    towed_at = {}          # index -> the minute it was towed off the gate

    def gate_spans():
        """The stretches of time each aircraft currently needs a gate for."""
        spans = []
        for index in range(count):
            if index in towed_at:
                spans.append((arrival_of[index], towed_at[index], index))
                spans.append((departure_of[index] - config.DEPARTURE_GATE_MINUTES,
                              departure_of[index], index))
            else:
                spans.append((arrival_of[index], departure_of[index], index))
        return spans

    def can_be_towed(index, minute):
        """An aircraft can only be moved once it has finished deplaning and
        while there is still real time before it has to board."""
        if index in towed_at:
            return False
        if arrival_of[index] + config.ARRIVAL_GATE_MINUTES > minute:
            return False
        if (departure_of[index] - config.DEPARTURE_GATE_MINUTES
                <= minute + config.MIN_STAND_MINUTES):
            return False
        return True

    # Repair until clean. Each pass measures the real occupancy of the blocks
    # as they currently stand, then tows aircraft at every moment that is over
    # capacity. Measuring from the blocks themselves - rather than trusting a
    # running simulation - is the point: an earlier version tracked occupancy
    # as it went, drifted out of step with the blocks it was producing, and
    # confidently reported a schedule that needed 63 gates as if it fitted in
    # 57. This cannot do that. If a pass finds nothing over capacity, nothing
    # is over capacity.
    MAX_PASSES = 40
    for _pass in range(MAX_PASSES):
        events = []
        for start_minute, end_minute, index in gate_spans():
            events.append((start_minute, 1, index))
            events.append((end_minute + buffer_minutes, -1, index))
        events.sort(key=lambda e: (e[0], e[1]))

        # Occupancy is the SIZE OF THE LIVE SET, never a separate counter.
        # Keeping a counter alongside the set is what broke the previous
        # version: towing decremented the counter, then the towed aircraft's
        # own end-of-span event decremented it a second time, and the count
        # drifted below reality until the planner believed a 73-gate day fitted
        # into 57. One source of truth, no drift.
        live = set()
        towed_this_pass = 0

        for minute, change, index in events:
            if change < 0:
                live.discard(index)
                continue

            live.add(index)

            while len(live) > gate_count:
                victim = None
                for candidate in live:
                    if not can_be_towed(candidate, minute):
                        continue
                    if victim is None or departure_of[candidate] > departure_of[victim]:
                        victim = candidate
                if victim is None:
                    # Nobody can be moved: everyone here is either still
                    # deplaning or already boarding. Genuinely over capacity
                    # for this moment.
                    break
                towed_at[victim] = minute
                live.discard(victim)
                towed_this_pass += 1

        if towed_this_pass == 0:
            break

    # --- turn the decisions into the blocks the solvers consume -------------
    blocks = []
    for index, turn in enumerate(turns.itertuples(index=False)):
        start = arrival_of[index]
        end = departure_of[index]

        if index not in towed_at:
            blocks.append({
                "turn_index": index,
                "tail_number": turn.tail_number,
                "start_minute": start,
                "end_minute": end,
                "block_type": "full",
            })
            continue

        blocks.append({
            "turn_index": index,
            "tail_number": turn.tail_number,
            "start_minute": start,
            "end_minute": towed_at[index],
            "block_type": "arrival",
        })
        blocks.append({
            "turn_index": index,
            "tail_number": turn.tail_number,
            "start_minute": end - config.DEPARTURE_GATE_MINUTES,
            "end_minute": end,
            "block_type": "departure",
        })

    return pd.DataFrame(blocks)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def peak_occupancy(intervals):
    """The largest number of aircraft on the ground at the same moment.

    This is the single most important sanity check in the whole pipeline,
    because it is a hard LOWER BOUND on the number of gates required. If this
    number exceeds the gate count, the assignment model is infeasible and no
    amount of solver tuning will help.

    The method is a sweep line: walk every start and end time in chronological
    order, adding one when an aircraft arrives and subtracting one when it
    leaves, and remember the highest the running count ever gets.
    """
    events = []
    for row in intervals.itertuples(index=False):
        events.append((row.start_minute, 1))
        events.append((row.end_minute, -1))

    # Sorting by (time, change) puts departures (-1) before arrivals (+1) at
    # the same minute, which is correct: a gate freed at 10:00 can be reused
    # by an aircraft arriving at 10:00.
    events.sort()

    running_count = 0
    highest_count = 0
    for _, change in events:
        running_count = running_count + change
        if running_count > highest_count:
            highest_count = running_count

    return highest_count
