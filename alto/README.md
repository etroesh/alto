# `alto/` — The Engine

**This is where all the thinking happens.** Every other folder either feeds this one or displays what it produced.

Read the files in this order:

| File | What it does |
|---|---|
| **`config.py`** | Every number the project depends on, in one place, each with a source. If you want to change what the model assumes, change it here — nowhere else. |
| **`gates.py`** | Defines the 57 gates Alaska can use at SEA: which concourse each is on, and how far a passenger has to walk to reach it. |
| **`build_turns.py`** | Turns raw flight records into aircraft ground visits. **Read the docstring at the top** — it explains the bug that broke the first version of this project. |
| **`build_db.py`** | Loads everything into `data/processed/alto.db`. Run it with `python -m alto.build_db`. |

| **`schedule.py`** | The three things both solvers need: pull one day out of the database, decide whether two blocks can share a gate, find the busiest moments. Shared on purpose — if the solvers worked from different data, comparing them would prove nothing. |
| **`solver_mcnf.py`** | **The fast solver the website uses.** Minimum-cost network flow. Answers in about 0.3 seconds. |
| **`solver_ilp.py`** | **The exact solver.** Integer programming. About 12 seconds. Used to prove the fast one is right, and to handle cases the fast one cannot. |

| **`scenarios.py`** | Describes a disruption: which aircraft are late, which gates are shut, what the cost assumptions are. Takes a normal day and produces the broken version of it. |
| **`costs.py`** | **The one the website's main screen calls.** Runs the day forward under the disruption, prices what it costs to do nothing, re-optimizes, and prices the recovery. |

## Why it's a "package"

The `__init__.py` file is what makes this folder importable as a unit. It's why other code can say `from alto import config` instead of juggling file paths. That's all it does.
