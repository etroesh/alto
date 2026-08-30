# ALTO — Aviation Logistics & Terminal Optimizer

Gate scheduling and disruption-cost analysis for Alaska Airlines at Seattle-Tacoma International Airport, built on U.S. Bureau of Transportation Statistics flight data.

**Live site:** [alto.troesh.net](https://alto.troesh.net) · **Methods:** [alto.troesh.net/methods.html](https://alto.troesh.net/methods.html)

---

## What this project does, in one paragraph

Alaska Airlines runs roughly 220 aircraft turns a day at Sea-Tac. Every one of those aircraft needs a gate, and gates are expensive, contractually constrained, and finite. ALTO takes a real day of flights, works out the cheapest legal way to assign them to gates, then lets you break that day — delay flights, close a gate, close a whole concourse — and shows you two things: **what the disruption costs if you do nothing**, and **how much of that you get back by re-optimizing.**

---

## Where everything lives

| Folder | Plain English | What's in it |
|---|---|---|
| **`alto/`** | **The engine.** All the code that does the actual thinking. | Reads the data, builds turns, solves the gate assignment, calculates costs. |
| **`api/`** | **The server.** The bridge between the engine and the website. | Receives a request from the website ("delay flight 123"), runs the engine, sends back the answer. |
| **`website/`** | **The website.** What a person sees at alto.troesh.net. | The charts, the controls, the Methods page. |
| **`data/`** | **The data.** | `processed/` holds `alto.db`, the SQLite database everything reads from. |
| **`scripts/`** | **Utilities.** Downloading the raw BTS files, invariant tests, solver validation. | Run these to reproduce the pipeline or check the solvers agree. |

---

## The pipeline, start to finish

```
  1. DOWNLOAD          scripts/download_bts_data.py
     12 monthly BTS files, every U.S. flight in 2023      → data/raw/

  2. FILTER            Alaska flights touching Seattle     → 139,000 flights

  3. BUILD TURNS       alto/build_turns.py
     Pair each arrival with the aircraft's next departure
     Decide which turns get towed to a hardstand          → gate blocks

  4. LOAD DATABASE     alto/build_db.py
     Everything into one SQLite file                      → data/processed/alto.db

  5. SOLVE             alto/solver_mcnf.py  (production, fast)
                       alto/solver_ilp.py   (reference, exact)
     Assign every gate block to a gate                    → assignments

  6. PRICE IT          alto/costs.py
     Delay cost, idle gate cost, downstream knock-ons     → dollars

  7. SERVE             api/
     Wrap all of the above in a web service

  8. SHOW              website/
     Charts and controls at alto.troesh.net
```

---

## How to run it

```bash
pip install -r requirements.txt

# Rebuild the database from scratch
python -m alto.build_db

# Check the gate roster looks right
python alto/gates.py
```

---

## Start here if you're reading the code for the first time

In this order:

1. **[The Methods page](https://alto.troesh.net/methods.html)** — why any of this matters, the formulation, the bugs, the validation. Read this first even though it's not code.
2. **`alto/config.py`** — every number the project depends on, each one with a source.
3. **`alto/build_turns.py`** — the module docstring tells the story of the bug that broke the first version.
4. **`alto/gates.py`** — what gates exist and what they cost to walk to.
