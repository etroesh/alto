"""
config.py - All the numbers and settings ALTO depends on, in one place.

Every constant that a reviewer might question lives here with a comment
explaining where it came from. Nothing in the rest of the codebase should
contain a "magic number" - if you find one, it belongs in this file.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# PROJECT_ROOT is the folder that contains the "alto" package, i.e. the repo
# root. Path(__file__) is this file; .parent is alto/; .parent.parent is root.
PROJECT_ROOT = Path(__file__).parent.parent

DATA_RAW = PROJECT_ROOT / "data" / "raw" / "bts_ontime"
DATA_PROCESSED = PROJECT_ROOT / "data" / "processed"
DATABASE_PATH = DATA_PROCESSED / "alto.db"

# The combined, already-filtered BTS file produced by 01_filter_bts.
# Carrier = AS, and Origin = SEA or Dest = SEA, all 12 months of 2023.
FILTERED_FLIGHTS_CSV = DATA_PROCESSED / "AS_SEA_all2023.csv"


# ---------------------------------------------------------------------------
# The airport and the airline we are modeling
# ---------------------------------------------------------------------------
AIRPORT = "SEA"          # Seattle-Tacoma International
CARRIER = "AS"           # Alaska Airlines
YEAR = 2023


# ---------------------------------------------------------------------------
# Turn construction rules
# ---------------------------------------------------------------------------
# A "turn" is one aircraft's ground visit at SEA: it arrives on one flight and
# leaves on the next flight that same tail number operates out of SEA.
#
# MIN_TURN_MINUTES: an aircraft physically cannot arrive and depart in under
# this many minutes. Anything shorter is a data error (usually a bad ArrTime or
# DepTime in the BTS file), not a real turn, so we drop it.
# Alaska's published minimum ground time for a 737 is around 35-40 minutes;
# we use 20 as a permissive floor so we only remove clear nonsense.
MIN_TURN_MINUTES = 20

# MAX_TURN_MINUTES: the longest ground visit we will keep as a single turn.
# 36 hours covers overnight parking (RON = "remain overnight") plus a maintenance
# day. Beyond that the tail almost certainly left SEA on a flight the BTS file
# doesn't show (a ferry flight, or a repositioning we filtered out), so pairing
# across that gap would invent a turn that never happened.
MAX_TURN_MINUTES = 36 * 60

# Aircraft get towed to a remote hardstand when a gate is NEEDED, not on a
# timer. See occupancy_intervals() in build_turns.py for why the timer version
# was wrong. These are the parameters that rule uses.

# Alaska holds 57 PREFERENTIAL-USE gates at SEA: Concourse C, the North
# Satellite, and a share of Concourse D. Preferential means Alaska has priority
# on them and already pays rent, so using one more costs nothing at the margin.
PREFERENTIAL_GATE_COUNT = 57

# It also has access to COMMON-USE gates when its own are full, billed per turn
# rather than rented. This is not a convenience added to make the model fit -
# measuring forced it. With 57 gates and nothing else, Alaska's own schedule is
# over capacity at the evening bank on 39 days of 2023, and by then every
# aircraft on a gate is either still deplaning or already boarding: there is
# nobody left to tow. The schedule simply needs more than 57 stands.
#
# SLOA V makes the South Concourse and all new construction common-use, so the
# capacity exists. Sixteen is an assumption, arrived at by measurement: it is
# the smallest allotment at which no day of 2023 is over capacity. It is
# priced, not free - every turn on one costs the common-use tariff.
COMMON_USE_GATE_COUNT = 16

GATE_COUNT = PREFERENTIAL_GATE_COUNT + COMMON_USE_GATE_COUNT

# The shortest time off-gate that justifies calling a tug. Moving an aircraft
# for twenty minutes costs more in ramp effort than the gate time is worth.
MIN_STAND_MINUTES = 30

# When a turn is towed, how long the aircraft holds a gate on each end.
ARRIVAL_GATE_MINUTES = 60      # deplaning, cleaning, servicing
DEPARTURE_GATE_MINUTES = 60    # boarding, pushback prep


# ---------------------------------------------------------------------------
# Cost parameters
# ---------------------------------------------------------------------------
# Everything below is a PUBLISHED figure with a source, not an estimate.
# If a number here changes, every dollar figure the website shows changes with
# it, so each one names where it came from.

# --- What a minute of delay costs the airline ------------------------------
# Airlines for America publishes the direct aircraft operating cost per block
# minute for U.S. passenger carriers every year. For 2025 it is $98.41,
# broken down as:
#     crew (pilots + flight attendants)  $37.01
#     fuel                               $29.34
#     maintenance                        $18.35
#     aircraft ownership                  $9.76
#     other                               $3.95
# Source: Airlines for America, "U.S. Passenger Carrier Delay Costs"
#         https://www.airlines.org/dataset/u-s-passenger-carrier-delay-costs/
# This is the airline's own cost only. It does NOT include the value of
# passengers' time, which A4A separately puts at about $47 per hour. We model
# the airline's cost because that is what an airline optimizing its own
# operation would minimize.
DELAY_COST_PER_MINUTE = 98.41


# --- What SEA actually charges Alaska --------------------------------------
# All figures from the Port of Seattle tariff effective 1 January 2025:
# https://www.portseattle.org/sites/default/files/2024-12/Tariff%20Effective%2001012025.pdf
# "Signatory" means an airline that has signed the long-term lease agreement
# (Alaska has). Signatory carriers pay roughly 20% less than everyone else.

LANDING_FEE_PER_1000_LBS = 4.89          # signatory; non-signatory is $6.11
GATE_RENTAL_PER_SQFT_PER_YEAR = 360.37   # signatory; non-signatory is $450.46

# What it costs to use a COMMON-USE gate for one turn. These matter because
# they are the only place the tariff puts an explicit price on a single use of
# a gate, which makes them the best available measure of what gate time is
# worth. Class is set by aircraft size.
COMMON_GATE_TURN_FEE_WIDEBODY = 1105.78     # Class 1
COMMON_GATE_TURN_FEE_NARROWBODY = 552.89    # Class 2, 100+ seats - Alaska's 737s
COMMON_GATE_TURN_FEE_REGIONAL = 276.45      # Class 3, 100 or fewer seats

# Parking an aircraft away from a passenger gate.
REMOTE_HARDSTAND_FEE = 100.00               # per use, up to 4 hours
RON_FEE_PER_12_HOURS = 200.00               # first two 12-hour periods
RON_FEE_BEYOND_24_HOURS = 5000.00           # every 12-hour period after that

# --- Derived: what one minute of gate time is worth ------------------------
# The tariff prices a gate by the square foot per year, which tells us nothing
# about the value of one idle minute. So we derive it from the common-use
# per-turn fee instead, which IS a price for one use of one gate:
#
#     $552.89 per narrowbody turn  /  90 minutes (our median turn)
#     = $6.14 per minute of gate time
#
# That is the opportunity cost of a gate sitting empty: time Alaska is paying
# for and not using. It is an estimate built from a published price, which is
# the most defensible thing available - no public source prices idle gate time
# directly.
MEDIAN_TURN_MINUTES_FOR_PRICING = 90
GATE_IDLE_COST_PER_MINUTE = COMMON_GATE_TURN_FEE_NARROWBODY / MEDIAN_TURN_MINUTES_FOR_PRICING


# --- The rule that decides whether to tow -----------------------------------
# TOW_THRESHOLD_MINUTES above is not an arbitrary number. Towing an aircraft to
# a hardstand costs $100 and frees the gate for everything except the first and
# last hour of the visit. So towing pays for itself when:
#
#     (ground_minutes - 120) x $6.14  >  $100
#     ground_minutes  >  120 + 16  =  136 minutes
#
# We use 180 minutes rather than 136, which is deliberately conservative: it
# only tows when the case is clear, and it leaves room for the real-world costs
# a tariff does not price - tug crews, ramp congestion, and the risk of not
# getting the aircraft back to a gate in time for boarding.


# --- The Minimum Use Requirement (this is a hard contractual constraint) ----
# Under SLOA V, the Signatory Lease and Operating Agreement that took effect at
# SEA in 2025 and runs ten years, an airline holding a preferential-use gate
# must average at least 6 turns per day on that gate. Gates that fall below the
# minimum are taken back and made common-use for the remainder of the
# agreement. SEA has 67 preferential-use gates in total.
# Source: Port of Seattle Commission, SLOA V briefing, 10 December 2024.
#
# This is the most consequential number in this file. It turns "how few gates
# can Alaska use?" from an academic question into a contractual one: gates used
# too lightly are gates Alaska loses.
MINIMUM_USE_TURNS_PER_DAY = 6.0
PREFERENTIAL_GATES_AT_SEA = 67


# --- How delay spreads through the day --------------------------------------
# If a plane lands 30 minutes late and its turn has 20 minutes of slack, only
# 10 minutes push into the next departure. This factor scales what is left
# after slack is absorbed, because crews recover some of it on the ground.
# 0.75 means three quarters of the unabsorbed delay carries forward.
# This one IS an assumption - there is no published figure for it - so it is
# exposed as a slider on the site and the Methods page says so.
DELAY_PROPAGATION_FACTOR = 0.75

# How many legs down the aircraft's rotation we follow a delay. Delays damp
# out; past four legs the effect is smaller than the error in the estimate.
MAX_PROPAGATION_DEPTH = 4


# ---------------------------------------------------------------------------
# Solver settings
# ---------------------------------------------------------------------------
# Two aircraft cannot use the same gate back to back with zero gap. The ramp
# crew needs time between one aircraft pushing back and the next one arriving:
# marshalling, chocks, ground equipment repositioning. Ten minutes is a
# conservative operational buffer. It applies everywhere consistently - in the
# conflict test, in the network flow, and in the peak occupancy count - so all
# three ways of measuring gate demand stay comparable.
MIN_GATE_BUFFER_MINUTES = 10

# A modeling device, NOT a dollar figure. The optimizer has two goals that
# compete: use as few gates as possible, and waste as little gate time as
# possible. We want the gate count to win every time - one extra gate is always
# worse than any amount of idle time - so we price a gate high enough that no
# amount of idle time can outweigh it. This is called a lexicographic
# objective: rank the goals, don't blend them.
#
# Dollar results are reported separately from this number, so nothing the
# website shows is contaminated by it.
GATE_OPENING_PRIORITY_COST = 1_000_000
