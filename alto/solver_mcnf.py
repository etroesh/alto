"""
solver_mcnf.py - The production solver. Minimum-cost network flow.

HOW THIS FILE FITS
------------------
    schedule.py  ->  [ THIS FILE ]  ->  a gate assignment, in milliseconds
                     solver_ilp.py  ->  the same answer, in seconds or minutes

This is the solver the website calls. It has to be fast enough that moving a
slider feels instant, which rules out running an integer program on every
click.

THE IDEA, IN ONE PARAGRAPH
--------------------------
Forget gates for a moment and think about CHAINS. A gate holds one aircraft,
then another, then another - a chain of blocks through the day. If you can
build the whole day out of very few chains, you need very few gates, because
one gate serves one chain.

So "what is the fewest gates I need?" is the same question as "what is the
fewest chains I can cover every block with?" And that question - minimum path
cover - is a network flow problem, which computers solve in milliseconds
rather than minutes.

That is the whole trick, and it is why the website can re-optimize live.

WHAT IT CANNOT DO, AND WHY WE ALSO HAVE AN ILP
----------------------------------------------
Network flow only works here because it treats every gate as interchangeable.
Chains do not know which gate they end up at, so the flow model cannot express
"put this aircraft on Concourse C rather than the North Satellite." The moment
gates stop being interchangeable, this formulation breaks and the integer
program in solver_ilp.py is the honest answer.

We handle walking distance in a second step below, and we are explicit that
this makes it a two-stage approach rather than one exact optimization.
"""

import networkx as nx

from alto import config, schedule


# ===========================================================================
# BLOCK 1 - Turning the day into a flow network
# ===========================================================================
# Every block becomes TWO nodes, an "in" and an "out". Think of them as the
# block's front door and back door.
#
#   SOURCE ------> in_i        "block i starts a new chain, open a gate"
#   out_i  ------> SINK        "block i is the last one on its gate"
#   out_i  ------> in_j        "block j follows block i on the same gate"
#
# Each block needs exactly one unit of flow arriving at its front door, and
# exactly one leaving its back door. That forces every block to have exactly
# one predecessor slot and one successor slot filled, which is precisely what
# makes the result a set of clean, non-overlapping chains.
#
# The costs encode what we care about:
#   opening a gate  -> very expensive, so the solver opens as few as possible
#   linking blocks  -> costs the idle minutes wasted between them
#   ending a chain  -> free

SOURCE = "SOURCE"
SINK = "SINK"


# Every weight in the network is scaled up by this factor so that we have
# room to express a tie-break below one minute. networkx needs whole-number
# weights, and without the headroom a tie-break of "half a minute" is not
# expressible at all.
WEIGHT_SCALE = 10
STICKINESS_BONUS = 1        # one tenth of a minute, after scaling


def build_flow_network(blocks, previous_links=None):
    """Build the network described above from one day of gate blocks.

    previous_links, when given, is the set of (earlier, later) pairs that sat
    next to each other on the same gate in the plan we are recovering from.
    Those links get a tiny discount, which does not change what the optimal
    number of gates or the optimal idle time are, but does decide WHICH of
    the many equally-good arrangements the solver returns: the one closest to
    the plan already in place.

    This matters more than it sounds. Without it, a two-aircraft delay comes
    back as a plan that moves two hundred aircraft, all of them for no reason
    beyond the solver having picked a different equally-optimal answer. Ops
    cannot act on that, and neither can a person reading the website.
    """
    if previous_links is None:
        previous_links = set()
    network = nx.DiGraph()

    block_count = len(blocks)
    starts = list(blocks["start_minute"])
    ends = list(blocks["end_minute"])

    # networkx uses "demand" to mean net flow INTO a node: positive consumes,
    # negative produces. Only the block nodes carry a demand - each front door
    # must receive one unit, each back door must send one out.
    #
    # The source and sink are deliberately left at zero, joined by a return
    # pipe from SINK back to SOURCE. That pipe is what lets the solver DECIDE
    # how many chains to open. Every unit that travels round it is one chain
    # ending and another beginning, and it pays the gate-opening cost on the
    # way out of SOURCE.
    #
    # Getting this wrong is easy and the failure is silent: fixing the source
    # to emit one unit per block forces every block into its own chain, and
    # the model reports needing 289 gates for a 29-gate day.
    network.add_node(SOURCE, demand=0)
    network.add_node(SINK, demand=0)
    network.add_edge(SINK, SOURCE, capacity=block_count, weight=0)

    for position in range(block_count):
        network.add_node(("in", position), demand=1)
        network.add_node(("out", position), demand=-1)

        # Opening a gate for this block.
        network.add_edge(
            SOURCE, ("in", position),
            capacity=1,
            weight=config.GATE_OPENING_PRIORITY_COST * WEIGHT_SCALE,
        )
        # Letting the chain end here.
        network.add_edge(("out", position), SINK, capacity=1, weight=0)

    # Now the links: which blocks can follow which, and what that costs.
    for earlier in range(block_count):
        for later in range(block_count):
            if earlier == later:
                continue
            if not schedule.can_follow(ends[earlier], starts[later]):
                continue

            # Idle time is the gap beyond the buffer the ramp actually needs.
            # A perfectly tight connection scores zero.
            idle_minutes = starts[later] - ends[earlier] - config.MIN_GATE_BUFFER_MINUTES

            weight = int(idle_minutes) * WEIGHT_SCALE
            if (earlier, later) in previous_links:
                weight = weight - STICKINESS_BONUS

            network.add_edge(
                ("out", earlier), ("in", later),
                capacity=1,
                weight=weight,
            )

    return network


def links_from_assignment(blocks, previous_assignment):
    """Which blocks sat next to each other on a gate in the previous plan."""
    if not previous_assignment:
        return set()

    starts = list(blocks["start_minute"])

    blocks_at_gate = {}
    for position in previous_assignment:
        gate = previous_assignment[position]
        blocks_at_gate.setdefault(gate, []).append(position)

    links = set()
    for gate in blocks_at_gate:
        queue = sorted(blocks_at_gate[gate], key=lambda p: starts[p])
        for index in range(1, len(queue)):
            links.add((queue[index - 1], queue[index]))

    return links


# ===========================================================================
# BLOCK 2 - Reading the chains back out of the solved flow
# ===========================================================================
# The solver hands back how much flow went down every edge. We only care about
# the out_i -> in_j edges that carried flow: each one says "j follows i".
# Following those links from each chain's first block gives us the chains.

def extract_chains(flow_result, block_count):
    """Turn the solved flow into a list of chains, each a list of positions."""
    # follows[i] = j means block j comes after block i on the same gate.
    follows = {}
    has_predecessor = set()

    for from_node in flow_result:
        for to_node in flow_result[from_node]:
            units = flow_result[from_node][to_node]
            if units <= 0:
                continue
            # We only want the block-to-block links, not the source and sink.
            if not isinstance(from_node, tuple) or not isinstance(to_node, tuple):
                continue
            if from_node[0] != "out" or to_node[0] != "in":
                continue

            follows[from_node[1]] = to_node[1]
            has_predecessor.add(to_node[1])

    # A chain starts at any block nothing else leads into.
    chains = []
    for position in range(block_count):
        if position in has_predecessor:
            continue

        chain = [position]
        current = position
        while current in follows:
            current = follows[current]
            chain.append(current)
        chains.append(chain)

    return chains


# ===========================================================================
# BLOCK 3 - Putting the chains onto real, named gates
# ===========================================================================
# The flow model produced chains, not gate names, because it treats gates as
# interchangeable. This step names them, and it has TWO jobs depending on
# whether we are planning a fresh day or recovering from a disruption.
#
#   FRESH DAY   there is no previous plan, so name gates to minimize walking:
#               longest chains onto the closest gates.
#
#   RECOVERY    there IS a previous plan, and every aircraft we move away from
#               its original gate is a real operational cost - a re-broadcast,
#               a jet bridge crew repositioned, passengers walking to a
#               different gate. So name gates to keep as many aircraft where
#               they already were as possible.
#
# Both are the same underlying problem: pair each chain with a gate at the
# lowest total cost, where each gate takes at most one chain. That is the
# classic assignment problem, solved exactly and instantly by the Hungarian
# algorithm. Writing it once with a swappable cost matrix means the recovery
# case is not a special case at all.

import numpy as np
from scipy.optimize import linear_sum_assignment


def assign_chains_to_gates(chains, gates, previous_assignment=None,
                           blocks=None):
    """Give each chain a real gate.

    previous_assignment - optional {block position -> gate id} from the plan
                          we are recovering from. When supplied, the pairing
                          minimizes how many aircraft have to move.
    """
    gates_by_walk = gates.sort_values("walk_cost").reset_index(drop=True)
    gate_ids = list(gates_by_walk["gate_id"])
    walk_costs = list(gates_by_walk["walk_cost"])

    # A common-use stand is billed per turn. Walking cost alone put S gates at
    # roughly 7.6 against C's 1.0 - about four times worse - while the real
    # difference is $552.89 against nothing, because rent on Alaska's own
    # gates is already paid. Four-to-one is nowhere near enough, and the
    # consequence showed up on screen: a re-plan that used ONE fewer gate but
    # six more common-use turns was reported as an improvement, recovering
    # minus $21,010. The fee goes in the pairing cost so the solver reaches
    # for a common-use stand only when there is no leased gate left.
    common_use_fee = [
        config.COMMON_GATE_TURN_FEE_NARROWBODY if str(gate_id).startswith("S") else 0.0
        for gate_id in gate_ids
    ]

    if len(chains) > len(gate_ids):
        # More chains than gates. The caller reports this as infeasible
        # rather than silently dropping aircraft.
        return None

    # Build the cost of putting each chain on each gate.
    cost_matrix = np.zeros((len(chains), len(gate_ids)))

    for chain_index, chain in enumerate(chains):
        for gate_index, gate_id in enumerate(gate_ids):

            walking = len(chain) * walk_costs[gate_index]
            fees = len(chain) * common_use_fee[gate_index]

            if previous_assignment is None:
                cost_matrix[chain_index][gate_index] = walking + fees
                continue

            # How many aircraft in this chain would have to MOVE if we put
            # the chain here? Every block that was somewhere else counts.
            moves = 0
            for position in chain:
                was_at = previous_assignment.get(position)
                if was_at is not None and was_at != gate_id:
                    moves = moves + 1

            # A move is worth far more than a unit of walking, so moves decide
            # the pairing and walking only breaks ties between equally
            # disruptive options.
            # Fees outrank moves: paying $552.89 a turn to avoid shuffling an
            # aircraft between two gates it is already parked at is not a
            # trade any airline would make.
            cost_matrix[chain_index][gate_index] = fees + moves * 1000.0 + walking

    chain_rows, gate_columns = linear_sum_assignment(cost_matrix)

    assignment = {}
    for chain_index, gate_index in zip(chain_rows, gate_columns):
        gate_id = gate_ids[gate_index]
        for position in chains[chain_index]:
            assignment[position] = gate_id

    return assignment


# ===========================================================================
# BLOCK 4 - The whole thing, start to finish
# ===========================================================================

def solve(blocks, gates, previous_assignment=None):
    """Assign every gate block to a gate. Returns a result dictionary.

    previous_assignment, when given, makes this a RECOVERY solve: same optimal
    number of gates, but arranged to move as few aircraft as possible from
    where they already were.

    This is the function the API calls.
    """
    if len(blocks) == 0:
        return {"feasible": True, "gates_used": 0, "assignment": {},
                "chains": [], "total_idle_minutes": 0}

    # The free sanity check: the answer can never be smaller than this, and
    # for this problem it should be exactly this.
    minimum_possible_gates = schedule.peak_demand(blocks)

    if minimum_possible_gates > len(gates):
        return {
            "feasible": False,
            "reason": (
                "Peak demand is " + str(minimum_possible_gates) + " gates but only "
                + str(len(gates)) + " are available. No assignment exists."
            ),
            "gates_needed": minimum_possible_gates,
        }

    previous_links = links_from_assignment(blocks, previous_assignment)
    network = build_flow_network(blocks, previous_links)
    flow_result = nx.min_cost_flow(network)

    chains = extract_chains(flow_result, len(blocks))
    assignment = assign_chains_to_gates(chains, gates, previous_assignment)

    if assignment is None:
        return {"feasible": False, "reason": "More chains than gates.",
                "gates_needed": len(chains)}

    total_idle = total_idle_minutes(chains, blocks)

    return {
        "feasible": True,
        "gates_used": len(chains),
        "minimum_possible_gates": minimum_possible_gates,
        "assignment": assignment,
        "chains": chains,
        "total_idle_minutes": total_idle,
        "total_walk_cost": walk_cost_of(assignment, gates),
    }


def total_idle_minutes(chains, blocks):
    """Gate time paid for and not used, added up across every chain."""
    starts = list(blocks["start_minute"])
    ends = list(blocks["end_minute"])

    total = 0
    for chain in chains:
        for step in range(len(chain) - 1):
            earlier = chain[step]
            later = chain[step + 1]
            gap = starts[later] - ends[earlier] - config.MIN_GATE_BUFFER_MINUTES
            total = total + gap
    return total


def walk_cost_of(assignment, gates):
    """Total walking penalty across every assigned block."""
    cost_by_gate = {}
    for row in gates.itertuples(index=False):
        cost_by_gate[row.gate_id] = row.walk_cost

    total = 0.0
    for position in assignment:
        total = total + cost_by_gate[assignment[position]]
    return round(total, 2)
