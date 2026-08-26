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

def occupancy_intervals(turns, max_occupancy=None):
    """Work out which stretches of time each aircraft actually needs a gate for.

    THE RULE, AND WHY IT IS THIS ONE
    --------------------------------
    Seattle-Tacoma publishes a maximum gate occupancy. For an aircraft of 100
    to 199 seats - which covers Alaska's 737-800 and -900 - it is:

        120 minutes for a turnaround
         60 minutes if the aircraft only arrives
         75 minutes if it only departs

    Past that the airport charges a gate delay fee per 15-minute increment,
    rising to $1,000 once an aircraft is four hours over. Sitting at a gate all
    night would cost tens of thousands of dollars, so aircraft do not do it.
    Two independent industry sources say the same thing in plainer language: an
    aircraft going out of service for the day is parked on a remote stand, or
    at minimum pushed back off the gate.

    So a turn longer than the published maximum becomes TWO gate blocks - the
    arrival, then the aircraft is towed away, then it returns to board - with
    each block sized to the airport's own arrival-only and departure-only
    limits rather than to numbers we chose.

    WHAT WENT WRONG HERE ONCE, SO IT IS NOT REPEATED
    ------------------------------------------------
    This function briefly used a different rule: tow only when another aircraft
    needs the gate, on the reasoning that at three in the morning nobody wants
    it. That reasoning was never checked against a source. It was wrong on both
    counts - the tariff's occupancy limits and its Remain Overnight schedule
    are both built around aircraft NOT sitting at gates, and industry practice
    is to move them off. The evidence that looked damning at the time - gates
    sitting empty overnight while stands filled up - is simply what a hub looks
    like at 5am.

    Every parameter used here is now in docs/verification.md with its source.
    """
    if max_occupancy is None:
        max_occupancy = config.MAX_GATE_OCCUPANCY_MINUTES

    blocks = []
    for index, turn in enumerate(turns.itertuples(index=False)):
        if turn.ground_minutes <= max_occupancy:
            # Within the airport's limit. The aircraft holds its gate throughout.
            blocks.append({
                "turn_index": index,
                "tail_number": turn.tail_number,
                "start_minute": turn.arrival_minute,
                "end_minute": turn.departure_minute,
                "block_type": "full",
            })
            continue

        # Over the limit. Gate for the arrival, tow, gate again to board.
        blocks.append({
            "turn_index": index,
            "tail_number": turn.tail_number,
            "start_minute": turn.arrival_minute,
            "end_minute": turn.arrival_minute + config.ARRIVAL_GATE_MINUTES,
            "block_type": "arrival",
        })
        blocks.append({
            "turn_index": index,
            "tail_number": turn.tail_number,
            "start_minute": turn.departure_minute - config.DEPARTURE_GATE_MINUTES,
            "end_minute": turn.departure_minute,
            "block_type": "departure",
        })

    return pd.DataFrame(blocks)


def off_gate_minutes(turn, max_occupancy=None):
    """How long an aircraft spends away from a gate, if it is towed at all."""
    if max_occupancy is None:
        max_occupancy = config.MAX_GATE_OCCUPANCY_MINUTES
    if turn["ground_minutes"] <= max_occupancy:
        return 0
    return (turn["ground_minutes"]
            - config.ARRIVAL_GATE_MINUTES
            - config.DEPARTURE_GATE_MINUTES)


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
