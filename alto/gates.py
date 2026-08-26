"""
gates.py - The gate roster Alaska Airlines can use at SEA.

IMPORTANT HONESTY NOTE, and this belongs on the Methods page too:
The BTS On-Time Performance dataset does NOT contain gate assignments. No
public dataset does. So this roster is built from the published SEA terminal
layout (which gates exist, and which concourse they are on), and the
gate-level attributes below - which gates take which aircraft size - are a
MODELING ASSUMPTION, not measured data. We state that plainly rather than
implying a precision we do not have.

What IS well established:
  - Concourse C is Alaska's main concourse at SEA (gates C1-C27ish)
  - The North Satellite is Alaska's, reached by the underground train
  - Concourse D gates are shared, mostly with American Airlines

What we ASSUME:
  - Alaska has practical access to roughly 10 of the D gates
  - A handful of gates are sized for regional jets (E175) only
  - Walking cost is proportional to distance from the terminal's center
"""

from dataclasses import dataclass

from alto import config


# Aircraft size classes. Alaska's SEA operation is almost entirely narrowbody:
# Boeing 737 family flown by Alaska mainline, and Embraer E175 flown by
# Horizon Air and SkyWest under the Alaska Horizon brand.
SIZE_REGIONAL = "regional"       # E175 and similar
SIZE_NARROWBODY = "narrowbody"   # 737 family

# A gate sized for a 737 can also take an E175. The reverse is not true.
# This dictionary answers "can a gate of size X hold an aircraft of size Y?"
SIZE_FITS = {
    (SIZE_REGIONAL, SIZE_REGIONAL): True,
    (SIZE_REGIONAL, SIZE_NARROWBODY): False,   # E175 gate cannot take a 737
    (SIZE_NARROWBODY, SIZE_REGIONAL): True,    # 737 gate can take an E175
    (SIZE_NARROWBODY, SIZE_NARROWBODY): True,
}


@dataclass(frozen=True)
class Gate:
    """One physical gate.

    gate_id     - short label, e.g. "C12", "N4" or "S3"
    concourse   - "C", "N" (North Satellite), "D", or "S" (common use)
    size        - the largest aircraft class this gate can hold
    walk_cost   - relative penalty for putting a flight here, in "cost units".
                  Higher means further from the terminal center, which means
                  longer connection times for passengers. This is a soft
                  preference, not a hard constraint.
    """
    gate_id: str
    concourse: str
    size: str
    walk_cost: float


def build_gate_roster():
    """Return the full list of gates Alaska can use at SEA.

    Built with an explicit loop per concourse rather than a clever
    comprehension, so it is obvious what is being created and easy to edit
    when you want to change a concourse's size or walking cost.
    """
    gates = []

    # --- Concourse C: Alaska's home concourse, attached to the main terminal.
    # Closest to security and baggage claim, so the lowest walking cost.
    for number in range(1, 28):          # C1 through C27
        gate_id = "C" + str(number)
        # Assumption: the low-numbered C gates sit at the near end of the
        # concourse and the high-numbered ones at the far end, so walking cost
        # rises as the gate number rises.
        walk_cost = 1.0 + (number / 27.0)
        gates.append(Gate(gate_id, "C", SIZE_NARROWBODY, walk_cost))

    # --- North Satellite: Alaska-exclusive, but only reachable by the
    # underground train. Every gate here carries a flat travel penalty on top
    # of the walk within the satellite itself.
    TRAIN_PENALTY = 2.5
    for number in range(1, 21):          # N1 through N20
        gate_id = "N" + str(number)
        walk_cost = TRAIN_PENALTY + (number / 20.0)
        # Every gate in this roster is sized for a 737. That is deliberate:
        # we filtered the BTS data to IATA_CODE_Reporting_Airline == "AS",
        # which is Alaska mainline only. Horizon Air and SkyWest E175 flights
        # report under their own carrier codes (QX and OO) and are not in our
        # dataset, so there are no regional aircraft to park. The size
        # machinery above stays in place for when those carriers are added -
        # marking gates regional-only now would silently delete capacity based
        # on an assumption we cannot support.
        gates.append(Gate(gate_id, "N", SIZE_NARROWBODY, walk_cost))

    # --- Concourse D: shared with American Airlines. Alaska can use these but
    # would rather not, both because of the walk and because coordinating a
    # shared gate costs operational flexibility. The high walking cost makes
    # the optimizer treat these as overflow capacity, which is how they are
    # actually used.
    SHARED_GATE_PENALTY = 4.0
    for number in range(1, 11):          # D1 through D10
        gate_id = "D" + str(number)
        walk_cost = SHARED_GATE_PENALTY + (number / 10.0)
        gates.append(Gate(gate_id, "D", SIZE_NARROWBODY, walk_cost))

    # --- Common-use gates: the overflow, and they are not free.
    # Alaska does not rent these. It pays the Port of Seattle a per-turn fee
    # every time it uses one - $552.89 for a narrowbody under the 2025 tariff.
    # The optimizer therefore reaches for them only when its own gates are
    # full, which is exactly how a real airline treats them.
    COMMON_USE_PENALTY = 6.0
    for number in range(1, config.COMMON_USE_GATE_COUNT + 1):
        gate_id = "S" + str(number)
        walk_cost = COMMON_USE_PENALTY + (number / 10.0)
        gates.append(Gate(gate_id, "S", SIZE_NARROWBODY, walk_cost))

    return gates


def gate_can_hold(gate, aircraft_size):
    """True if this gate is big enough for this aircraft."""
    return SIZE_FITS[(gate.size, aircraft_size)]


# Running this file directly prints a summary, which is a fast way to sanity
# check the roster after editing it.
if __name__ == "__main__":
    roster = build_gate_roster()
    print("Total gates:", len(roster))
    for concourse in ["C", "N", "D", "S"]:
        in_concourse = [g for g in roster if g.concourse == concourse]
        regional = [g for g in in_concourse if g.size == SIZE_REGIONAL]
        print(
            "  Concourse " + concourse + ":",
            len(in_concourse), "gates,",
            len(regional), "regional-only,",
            "walk cost", round(min(g.walk_cost for g in in_concourse), 2),
            "to", round(max(g.walk_cost for g in in_concourse), 2),
        )
