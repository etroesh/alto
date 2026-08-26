"""
main.py - The web service. Everything the website asks for comes through here.

HOW THIS FILE FITS
------------------
    browser  ->  [ THIS FILE ]  ->  alto/schedule.py    get the day
                                ->  alto/scenarios.py   break the day
                                ->  alto/solver_mcnf.py assign gates
                                ->  alto/costs.py       price it

This file contains NO modeling logic of its own, deliberately. It translates
web requests into calls on the same modules the notebooks use, and translates
the answers back into JSON. If a number appears on the website, it was
produced by the same code you can run in a notebook - there is no second
implementation that could quietly drift.

THE ENDPOINTS
-------------
    GET  /api/health              is the service alive
    GET  /api/gates               the gate roster
    GET  /api/days                every date available, with a summary
    GET  /api/day/{date}          one day: blocks, gates, optimal assignment
    POST /api/optimize            a scenario in, damage and recovery out
"""

import contextlib
import math
import sys
import threading
import time
from pathlib import Path

# Let this file find the alto package when run from anywhere.
sys.path.insert(0, str(Path(__file__).parent.parent))

import sqlite3

from fastapi import FastAPI, HTTPException
from pydantic import field_validator
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from alto import config, costs, scenarios, schedule, solver_ilp, solver_mcnf


app = FastAPI(
    title="ALTO",
    description="Gate scheduling and disruption cost analysis for Alaska Airlines at SEA",
    version="1.0",
    # FastAPI builds a browsable page of every endpoint, with a button to run
    # each one. By default it lives at /docs - but the web server in front only
    # forwards paths beginning with /api/, so the default is unreachable from
    # outside. Moving these three under /api/ puts them back in reach.
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

# The website is served from the same host, but allowing any origin means the
# API also works from a local file while developing, and from the artifact
# preview. There is nothing private here - it is public flight data.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ===========================================================================
# BLOCK 1 - Caching, bounded on purpose
# ===========================================================================
# Loading a day and solving its baseline takes about half a second, and every
# scenario request needs the baseline to compare against. So we cache.
#
# But an UNBOUNDED cache is dangerous here, and it was measured, not guessed:
#
#       just started            140 MB
#       after   1 day cached    156 MB
#       after  10 days cached   297 MB
#       after  40 days cached   357 MB
#       after 120 days cached   641 MB
#
# This service shares a 1 GB machine with a production site. An unbounded
# cache would eventually trigger the Linux out-of-memory killer, and the OOM
# killer does not necessarily kill the process that caused the problem - it
# kills whatever it judges most expensive. That could be the other site.
#
# So the cache keeps only the most recently used days and evicts the oldest.
# Visitors look at a handful of days in a session, so the hit rate stays high
# while memory stays flat and predictable. The systemd unit sets a hard
# ceiling on top of this, as a second line of defence.

import ctypes
import gc
from collections import OrderedDict

# THE BUG THIS REPLACES, BECAUSE IT COST AN OUTAGE
# -------------------------------------------------
# The cache used to hold eight days, with a comment saying that came to about
# 260 MB resident. The systemd unit sets MemoryHigh=220M. Those two numbers
# were written at different times and nobody put them side by side, so the
# service was configured to grow straight through its own ceiling.
#
# Crossing MemoryHigh does not kill a process - the kernel throttles the whole
# cgroup into constant reclaim instead. So the service stayed "active
# (running)" with a healthy log and simply stopped answering, which is a far
# worse failure than a crash: systemd sees nothing wrong and never restarts it.
#
# The fix is to stop keeping the two numbers in separate files. The cache now
# reads the real ceiling out of its own cgroup at run time and evicts to stay
# under a fraction of it. The day count below is only a second line of defence.
MAX_CACHED_DAYS = 4

# Used only if the cgroup cannot be read - a plain Mac, a container without
# limits, a future init system. Deliberately below the 220 MB the unit sets.
FALLBACK_MEMORY_BUDGET_MB = 170.0

# How much of the ceiling the caches may occupy. The rest is headroom for
# actually solving a day, which is the part that spikes.
MEMORY_BUDGET_SHARE = 0.75

_day_cache = OrderedDict()
_baseline_cache = OrderedDict()
_gates_cache = None

# Only one exact solve at a time.
#
# FastAPI runs a plain `def` endpoint in a thread pool, so two visitors ticking
# "solve exactly" really do solve at once. Measured, each one holds roughly
# 50 MB that eviction cannot reclaim while it runs - two together clear the
# 220 MB ceiling and throttle the whole service to a standstill. Queueing them
# costs the second visitor some waiting; not queueing them costs everyone the
# service. The network flow is untouched: it is a second's work and runs freely.
_exact_solve_lock = threading.Lock()


def _release_memory_to_os():
    """Hand freed memory back to the operating system.

    Python frees memory internally but does not always return it to the OS -
    it keeps it for next time. That is usually a sensible optimisation. Here it
    is not: solving a day builds a network with tens of thousands of edges, and
    the leftovers accumulate until this process is holding half a gigabyte on a
    machine that only has one, shared with a live site.

    malloc_trim is a glibc call that releases those unused arenas back. It is
    Linux-specific, so it is wrapped in a try - on any other system this
    quietly does nothing and the systemd memory ceiling is the backstop.
    """
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def _own_cgroup_directory():
    """Where this process's own cgroup limits live on disk.

    THE MISTAKE THIS EXISTS TO CORRECT
    ----------------------------------
    The first version read /sys/fs/cgroup/memory.high directly and always got
    the fallback. That path is the limit of the cgroup ROOT, which is "max" -
    the root is unlimited by definition. A systemd service's own limits sit
    further down, at /sys/fs/cgroup/system.slice/alto-api.service/memory.high.

    /proc/self/cgroup names the path. Under cgroup v2 it is a single line:

        0::/system.slice/alto-api.service

    So the directory is /sys/fs/cgroup + that suffix. It was caught because
    /api/health reported the fallback figure on a machine that definitely has
    a limit set - which is exactly why the budget is reported there.
    """
    try:
        with open("/proc/self/cgroup") as cgroup_file:
            for line in cgroup_file:
                parts = line.strip().split(":", 2)
                if len(parts) == 3 and parts[0] == "0":       # "0" means cgroup v2
                    return "/sys/fs/cgroup" + parts[2]
    except OSError:
        pass
    return None


def _memory_budget_mb():
    """How much memory the caches are allowed to hold, in megabytes.

    Read from the service's own cgroup rather than from a number typed into
    this file, so the cache can never again be configured to grow past the
    limit systemd will enforce on it. cgroup v2 exposes MemoryHigh as
    memory.high and MemoryMax as memory.max; each is a byte count, or the
    literal "max" when unset. Anything unreadable falls back to the constant
    above, which is deliberately below what the unit file sets.
    """
    directory = _own_cgroup_directory()
    candidates = []
    if directory:
        candidates += [directory + "/memory.high", directory + "/memory.max"]
    # A container may put the limits at the root instead; try there too.
    candidates += ["/sys/fs/cgroup/memory.high", "/sys/fs/cgroup/memory.max"]

    for path in candidates:
        try:
            with open(path) as limit_file:
                raw = limit_file.read().strip()
            if raw and raw != "max":
                return (int(raw) / 1024 / 1024) * MEMORY_BUDGET_SHARE
        except (OSError, ValueError):
            continue
    return FALLBACK_MEMORY_BUDGET_MB


def _memory_budget_source():
    """Whether the budget came from the real cgroup limit or from the fallback.

    Reported on /api/health so "is the ceiling actually being read?" is a
    question that can be answered from outside the machine. It could not be,
    the first time, and that is how the bug above survived a deploy.
    """
    directory = _own_cgroup_directory()
    for path in ([directory + "/memory.high", directory + "/memory.max"] if directory else []) \
            + ["/sys/fs/cgroup/memory.high", "/sys/fs/cgroup/memory.max"]:
        try:
            with open(path) as limit_file:
                raw = limit_file.read().strip()
            if raw and raw != "max":
                return path
        except (OSError, ValueError):
            continue
    return "fallback (no cgroup limit readable)"


def _trim_to_memory_budget():
    """Drop cached work until this process fits back inside its budget.

    Evicts baselines before days: a baseline is one solve away from being
    rebuilt, while a day is a database read plus a DataFrame. Always leaves one
    of each, so the request in flight still has what it needs.
    """
    budget = _memory_budget_mb()
    for _ in range(len(_baseline_cache) + len(_day_cache)):
        resident = _resident_memory_mb()
        if resident is None or resident <= budget:
            return
        if len(_baseline_cache) > 1:
            _baseline_cache.popitem(last=False)
        elif len(_day_cache) > 1:
            _day_cache.popitem(last=False)
        else:
            return                     # nothing left worth dropping
        _release_memory_to_os()


def _remember(cache, key, value):
    """Store a value, evicting the least recently used if the cache is full."""
    cache[key] = value
    cache.move_to_end(key)
    evicted = False
    while len(cache) > MAX_CACHED_DAYS:
        cache.popitem(last=False)      # last=False pops the OLDEST entry
        evicted = True
    if evicted:
        _release_memory_to_os()
    # The day count is a rough guard; actual memory is the one that matters.
    _trim_to_memory_budget()
    return value


def get_gates():
    """The gate roster. Tiny and constant, so this one is never evicted."""
    global _gates_cache
    if _gates_cache is None:
        _gates_cache = schedule.load_gates()
    return _gates_cache


def get_day(date_string):
    """One day's gate blocks."""
    if date_string in _day_cache:
        _day_cache.move_to_end(date_string)
        return _day_cache[date_string]

    blocks = schedule.load_day(date_string)
    if len(blocks) == 0:
        raise HTTPException(
            status_code=404,
            detail="No flights found for " + date_string
            + ". The dataset covers 2023-01-01 to 2023-12-31.",
        )
    return _remember(_day_cache, date_string, blocks)


def get_baseline(date_string, exact=False):
    """The optimal plan for an undisrupted day.

    Cached per solver, not just per date. The two solvers agree on the number
    of gates but not on the exact arrangement, so a baseline solved one way is
    not a valid comparison for a recovery solved the other way.
    """
    key = date_string + ("|exact" if exact else "|flow")
    if key in _baseline_cache:
        _baseline_cache.move_to_end(key)
        return _baseline_cache[key]

    blocks = get_day(date_string)
    solver = solver_ilp if exact else solver_mcnf
    solved = solver.solve(blocks, get_gates())
    return _remember(_baseline_cache, key, solved)


# ===========================================================================
# BLOCK 2 - Simple lookups
# ===========================================================================

def _resident_memory_mb():
    """How much memory this process is using right now, in megabytes.

    Read straight from the Linux process table. Exposed on /api/health so the
    memory ceiling can be checked from outside without logging into the box -
    which matters because this service shares a machine with a live site.
    """
    try:
        with open("/proc/self/status") as status_file:
            for line in status_file:
                if line.startswith("VmRSS:"):
                    kilobytes = int(line.split()[1])
                    return round(kilobytes / 1024, 1)
    except (OSError, ValueError):
        return None
    return None


@app.get("/api/health")
def health():
    """Is the service up, and is the database where it should be?"""
    database_present = config.DATABASE_PATH.exists()
    return {
        "status": "ok" if database_present else "database missing",
        "database": str(config.DATABASE_PATH),
        "days_cached": len(_day_cache),
        "baselines_cached": len(_baseline_cache),
        "cache_limit": MAX_CACHED_DAYS,
        "resident_memory_mb": _resident_memory_mb(),
        "memory_budget_mb": round(_memory_budget_mb(), 1),
        "memory_budget_from": _memory_budget_source(),
    }


@app.get("/api/gates")
def gates():
    """The gate roster: which gates exist, where they are, what they cost to reach."""
    return {"gates": get_gates().to_dict(orient="records")}


@app.get("/api/days")
def days():
    """Every date in the dataset with a one-line summary.

    This powers the date picker and the year-at-a-glance chart. It reads
    straight from SQL rather than solving anything, so it is fast.
    """
    connection = sqlite3.connect(config.DATABASE_PATH)
    rows = connection.execute("""
        SELECT arrival_date,
               COUNT(*) AS blocks,
               COUNT(DISTINCT tail_number) AS aircraft
        FROM gate_blocks
        GROUP BY arrival_date
        ORDER BY arrival_date
    """).fetchall()
    connection.close()

    return {
        "days": [
            {"date": r[0], "blocks": r[1], "aircraft": r[2]}
            for r in rows
        ]
    }


# ---------------------------------------------------------------------------
# Missing values, made safe for JSON
# ---------------------------------------------------------------------------
# An arrival-only visit has no departure flight, and a departure-only visit has
# no arrival flight. pandas stores those gaps as NaN, and NaN is not legal JSON
# - json.dumps writes the bare token NaN, which every browser refuses to parse.
# So every field that can be missing goes through one of these two helpers on
# the way out of the API, and comes out as a proper null.

def _is_missing(value):
    if value is None:
        return True
    return isinstance(value, float) and math.isnan(value)


def _clean_text(value):
    """A string field, or None if it was never there."""
    return None if _is_missing(value) else value


def _clean_number(value):
    """A whole-number field, or None if it was never there."""
    return None if _is_missing(value) else int(value)


# ===========================================================================
# BLOCK 3 - One day, optimally assigned
# ===========================================================================

@app.get("/api/day/{date_string}")
def day(date_string: str):
    """Everything the gate chart needs to draw one undisrupted day."""
    blocks = get_day(date_string)
    baseline = get_baseline(date_string)

    if not baseline["feasible"]:
        raise HTTPException(status_code=422, detail=baseline.get("reason"))

    assignment = baseline["assignment"]

    drawable = []
    for position in range(len(blocks)):
        row = blocks.iloc[position]
        drawable.append({
            "block_id": int(row["block_id"]),
            "turn_id": int(row["turn_id"]),
            "tail": row["tail_number"],
            "start": int(row["start_minute"]),
            "end": int(row["end_minute"]),
            "type": row["block_type"],
            "gate": assignment.get(position),
            "from": _clean_text(row["arrival_origin"]),
            "to": _clean_text(row["departure_dest"]),
            "arrival_flight": _clean_number(row["arrival_flight"]),
            "departure_flight": _clean_number(row["departure_flight"]),
            "ground_minutes": int(row["ground_minutes"]),
        })

    return {
        "date": date_string,
        "blocks": drawable,
        "gates_used": baseline["gates_used"],
        "minimum_possible_gates": baseline["minimum_possible_gates"],
        "gates_available": len(get_gates()),
        "total_idle_minutes": baseline["total_idle_minutes"],
    }


# ===========================================================================
# BLOCK 4 - The scenario endpoint, which is the whole point of the site
# ===========================================================================

class ScenarioRequest(BaseModel):
    """What the browser sends when someone changes something.

    Every field is optional. An empty scenario is a normal day, which is a
    useful thing to be able to ask for.
    """
    date: str
    delays: dict = {}                    # tail number -> minutes late
    closed_gates: list = []
    closed_concourses: list = []
    cost_overrides: dict = {}
    use_exact_solver: bool = False       # run the ILP instead of the flow model

    @field_validator("delays")
    @classmethod
    def delays_must_be_sensible(cls, value):
        """Reject nonsense before it reaches the model.

        ALTO models delays, not early arrivals. A negative value would run
        without error and quietly produce a result nobody asked for, so we
        refuse it with a message that says why. The upper bound is a day -
        beyond that the aircraft belongs to tomorrow's schedule.
        """
        for tail in value:
            minutes = value[tail]
            if not isinstance(minutes, (int, float)):
                raise ValueError("Delay for " + str(tail) + " must be a number.")
            if minutes < 0:
                raise ValueError(
                    "Delay for " + str(tail) + " is negative. ALTO models "
                    "delays, not early arrivals."
                )
            if minutes > 1440:
                raise ValueError(
                    "Delay for " + str(tail) + " is over 24 hours. An aircraft "
                    "that late belongs to the next day's schedule."
                )
        return value


@app.post("/api/optimize")
def optimize(request: ScenarioRequest):
    """Price the disruption, re-optimize, and report both.

    This returns three runs - baseline, damage, recovery - plus the list of
    which aircraft moved and where to. The gap between damage and recovery is
    the number the site exists to show.
    """
    started = time.time()

    blocks = get_day(request.date)

    scenario = {
        "date": request.date,
        "delays": request.delays,
        "closed_gates": request.closed_gates,
        "closed_concourses": request.closed_concourses,
        "cost_overrides": request.cost_overrides,
    }

    # The exact solver is offered deliberately. It finds a slightly better
    # arrangement for passenger walking - about 9 percent on average - at
    # roughly forty times the run time. Letting the visitor try both makes the
    # trade-off visible instead of hiding it.
    solver = solver_ilp if request.use_exact_solver else solver_mcnf

    # The lock has to cover the BASELINE as well as the recovery. Getting this
    # wrong once is instructive: with only the recovery guarded, two visitors
    # on two different days both solved an uncached baseline exactly, at the
    # same time, outside the lock - measured peak 247 MB against a 220 MB
    # ceiling. The expensive work is any exact solve, not just the last one.
    guard = _exact_solve_lock if request.use_exact_solver else contextlib.nullcontext()
    with guard:
        # Hand in the cached baseline so only the recovery has to be solved.
        baseline = get_baseline(request.date, request.use_exact_solver)
        result = costs.damage_and_recovery(
            blocks, get_gates(), scenario, solver, baseline_solution=baseline
        )

    # Solving builds a network of tens of thousands of edges, or a whole
    # integer program. Hand the leftovers back now rather than carrying them
    # until the next eviction happens to fire, and drop cached days if the
    # solve has pushed the process over its budget.
    _release_memory_to_os()
    _trim_to_memory_budget()

    if not result["feasible"]:
        raise HTTPException(
            status_code=422,
            detail={
                "stage": result.get("stage"),
                "reason": result.get("reason"),
            },
        )

    # Solving builds and discards a large network. Give the memory back before
    # answering, so a burst of requests cannot ratchet this process upward.
    _release_memory_to_os()

    # Shape the recovered plan for the chart, so the browser gets the money
    # AND the picture from a single request. Two requests meant two solves,
    # and with the exact solver that was twenty wasted seconds every time.
    disrupted = result.pop("disrupted_blocks")
    assignment = result.pop("recovery_assignment")
    gate_before = result.pop("gate_before_by_block_id")

    drawable = []
    for position in range(len(disrupted)):
        row = disrupted.iloc[position]
        block_id = int(row["block_id"])
        drawable.append({
            "block_id": block_id,
            "tail": row["tail_number"],
            "start": int(row["start_minute"]),
            "end": int(row["end_minute"]),
            "type": row["block_type"],
            "gate": assignment.get(position),
            "was_at_gate": gate_before.get(block_id),
            "injected_delay": int(row.get("injected_delay", 0)),
            "from": _clean_text(row.get("arrival_origin")),
            "to": _clean_text(row.get("departure_dest")),
        })
    result["blocks"] = drawable
    result["gates_available"] = len(scenarios.available_gates(
        get_gates(), request.closed_gates, request.closed_concourses))

    result["solver"] = "integer program" if request.use_exact_solver else "network flow"
    result["seconds"] = round(time.time() - started, 3)
    return result


@app.post("/api/assignment")
def assignment(request: ScenarioRequest):
    """The recovered gate plan itself, shaped for the chart.

    /api/optimize returns the money. This returns the picture: where every
    aircraft ends up after re-optimizing, so the Gantt can redraw.
    """
    blocks = get_day(request.date)
    disrupted = scenarios.apply_delays(blocks, request.delays)
    open_gates = scenarios.available_gates(
        get_gates(), request.closed_gates, request.closed_concourses
    )

    with (_exact_solve_lock if request.use_exact_solver else contextlib.nullcontext()):
        baseline = get_baseline(request.date)
    gate_by_block_id = {}
    for position in baseline["assignment"]:
        block_id = blocks.loc[position, "block_id"]
        gate_by_block_id[block_id] = baseline["assignment"][position]

    previous = {}
    open_gate_ids = set(open_gates["gate_id"])
    for position in range(len(disrupted)):
        block_id = disrupted.loc[position, "block_id"]
        was = gate_by_block_id.get(block_id)
        if was is not None and was in open_gate_ids:
            previous[position] = was

    solver = solver_ilp if request.use_exact_solver else solver_mcnf
    guard = _exact_solve_lock if request.use_exact_solver else contextlib.nullcontext()
    with guard:
        solution = solver.solve(disrupted, open_gates, previous_assignment=previous)
    _release_memory_to_os()
    _trim_to_memory_budget()

    if not solution["feasible"]:
        raise HTTPException(status_code=422, detail=solution.get("reason"))

    drawable = []
    for position in range(len(disrupted)):
        row = disrupted.iloc[position]
        block_id = int(row["block_id"])
        drawable.append({
            "block_id": block_id,
            "tail": row["tail_number"],
            "start": int(row["start_minute"]),
            "end": int(row["end_minute"]),
            "type": row["block_type"],
            "gate": solution["assignment"].get(position),
            "was_at_gate": gate_by_block_id.get(block_id),
            "injected_delay": int(row.get("injected_delay", 0)),
        })

    return {
        "date": request.date,
        "blocks": drawable,
        "gates_used": solution["gates_used"],
        "gates_available": len(open_gates),
    }
