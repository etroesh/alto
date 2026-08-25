"""
schedule.py - Shared building blocks for both solvers.

HOW THIS FILE FITS
------------------
    build_db.py  ->  alto.db  ->  [ THIS FILE ]  ->  solver_mcnf.py
                                                 ->  solver_ilp.py

Both solvers need exactly the same three things before they can do anything:

    1. the gate blocks for one day, pulled out of the database
    2. a rule for whether two blocks can share a gate
    3. the list of moments when the airport is busiest

This file provides those three things and nothing else. Keeping them here
rather than in each solver is what makes the cross-validation meaningful: when
the two solvers agree, we know it isn't because one of them was quietly
working from different data.
"""

import sqlite3

import pandas as pd

from alto import config


# ===========================================================================
# BLOCK 1 - Getting one day out of the database
# ===========================================================================

def load_day(date_string, database_path=None):
    """Pull every gate block for one calendar day.

    date_string is "YYYY-MM-DD", e.g. "2023-07-15".

    Returns a DataFrame with one row per block. A block is a stretch of time
    that needs a gate - which is not the same as a turn, because a long turn
    gets split into an arrival block and a departure block when the aircraft
    is towed away in between.
    """
    if database_path is None:
        database_path = config.DATABASE_PATH

    connection = sqlite3.connect(database_path)
    query = """
        SELECT block_id, turn_id, tail_number, start_minute, end_minute, block_type
        FROM gate_blocks
        WHERE arrival_date = ?
        ORDER BY start_minute
    """
    blocks = pd.read_sql(query, connection, params=[date_string])
    connection.close()

    # Both solvers refer to blocks by position (0, 1, 2, ...) rather than by
    # database id, because that is what array-shaped solver code expects.
    # Resetting the index here means position and row number always agree.
    blocks = blocks.reset_index(drop=True)
    return blocks


def load_gates(database_path=None):
    """Pull the gate roster out of the database."""
    if database_path is None:
        database_path = config.DATABASE_PATH

    connection = sqlite3.connect(database_path)
    gates = pd.read_sql("SELECT * FROM gates ORDER BY walk_cost", connection)
    connection.close()
    return gates.reset_index(drop=True)


# ===========================================================================
# BLOCK 2 - The rule that decides whether two blocks can share a gate
# ===========================================================================
# This single rule is the heart of the whole model, and both solvers must use
# it identically or the comparison between them means nothing.

def can_follow(earlier_end, later_start):
    """True if an aircraft leaving at earlier_end frees the gate in time for
    an aircraft arriving at later_start.

    The buffer is the ramp turnaround time between two aircraft: one has to
    push back and clear the area before the next can be marshalled in.
    """
    return later_start >= earlier_end + config.MIN_GATE_BUFFER_MINUTES


def blocks_conflict(block_a, block_b):
    """True if two blocks CANNOT share a gate.

    Two blocks are compatible only if one finishes far enough ahead of the
    other to start. If neither can follow the other, they overlap in time and
    conflict.
    """
    a_then_b = can_follow(block_a["end_minute"], block_b["start_minute"])
    b_then_a = can_follow(block_b["end_minute"], block_a["start_minute"])
    return not (a_then_b or b_then_a)


# ===========================================================================
# BLOCK 3 - Finding the busiest moments
# ===========================================================================
# Both solvers need to know which blocks are on the ground simultaneously.
# There is a shortcut here worth understanding, because it is what makes the
# integer program solvable at all.
#
# Naively you would write one constraint for every PAIR of conflicting blocks,
# for every gate. On a busy day that is tens of thousands of constraints and
# the solver crawls.
#
# But gate occupancy over time is what mathematicians call an INTERVAL GRAPH,
# and interval graphs have a useful property: every group of mutually
# overlapping blocks is already fully present at the moment one of them
# starts. So instead of enumerating pairs, we walk the block start times and
# record who is on the ground at each one. That gives us a handful of groups
# instead of thousands of pairs, and each group is a STRONGER constraint -
# "at most one of these twenty-seven blocks may use this gate" says more, in
# one line, than three hundred and fifty separate pairwise statements.

def overlap_groups(blocks):
    """Return the groups of blocks that are all on the ground together.

    Each group is a list of block positions. Every block in a group conflicts
    with every other block in that group, so at most one of them can be at any
    given gate.
    """
    groups = []
    seen_groups = set()

    starts = list(blocks["start_minute"])
    ends = list(blocks["end_minute"])

    for moment in starts:
        # Which blocks are occupying a gate at this exact moment? A block
        # counts if it has started and has not yet released the gate,
        # including its buffer.
        occupying = []
        for position in range(len(blocks)):
            has_started = starts[position] <= moment
            still_there = moment < ends[position] + config.MIN_GATE_BUFFER_MINUTES
            if has_started and still_there:
                occupying.append(position)

        # Groups of one tell the solver nothing, and the same group often
        # appears at several consecutive start times, so skip duplicates.
        if len(occupying) < 2:
            continue

        signature = tuple(occupying)
        if signature in seen_groups:
            continue

        seen_groups.add(signature)
        groups.append(set(occupying))

    # Many of these groups sit entirely inside a larger one. A group that is a
    # subset of another says nothing the bigger group does not already say, so
    # we keep only the MAXIMAL ones. Fewer constraints, and each is as strong
    # as it can be.
    maximal_groups = []
    for group in groups:
        is_inside_another = False
        for other in groups:
            if group is not other and group < other:   # "<" means proper subset
                is_inside_another = True
                break
        if not is_inside_another:
            maximal_groups.append(sorted(group))

    return maximal_groups


def peak_demand(blocks):
    """The largest number of blocks needing a gate at the same time.

    This is a HARD LOWER BOUND on the number of gates required, and for this
    kind of problem it is also the exact answer. Gate occupancy forms an
    interval graph, and interval graphs have the property that the minimum
    number of "colours" needed equals the size of the largest overlapping
    group. So the smallest possible number of gates IS this number.

    That gives us a free, independent check on both solvers: whatever they
    return for gates used, it must equal this. If it doesn't, something is
    wrong with the solver, not with the airport.
    """
    events = []
    for row in blocks.itertuples(index=False):
        events.append((row.start_minute, 1))
        events.append((row.end_minute + config.MIN_GATE_BUFFER_MINUTES, -1))

    # Sorting by (time, change) puts releases (-1) before claims (+1) at the
    # same minute, which is right: a gate freed at 10:00 is available at 10:00.
    events.sort()

    running = 0
    highest = 0
    for _, change in events:
        running = running + change
        if running > highest:
            highest = running

    return highest
