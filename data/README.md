# `data/` — The Data

## `raw/`
The original BTS On-Time Performance downloads: one CSV per month of 2023, about 3 GB total. Every commercial flight in the United States.

**Not stored in git** — too big, and re-downloadable with `scripts/download_bts_data.py`.

## `processed/`
| File | What it is |
|---|---|
| `AS_SEA_all2023.csv` | The 139,000 Alaska flights that touched Seattle in 2023. Produced by `notebooks/01_filter_bts.ipynb`. |
| `alto.db` | **The database everything reads from.** Produced by `alto/build_db.py`. Rebuild it any time with `python -m alto.build_db`. |

### What's inside `alto.db`

| Table | Rows | What it holds |
|---|---|---|
| `turns` | 75,347 | One row per aircraft ground visit: when it landed, when it left, which tail. |
| `gate_blocks` | 97,377 | The blocks of time that actually need a gate. More rows than turns, because long visits get split when the aircraft is towed away and brought back. |
| `gates` | 57 | The gate roster. |
| `assignments` | — | Which block went to which gate, per scenario. Filled by the solver. |
| `build_log` | 12 | Every setting used to build this database, so any number is reproducible. |

You can open `alto.db` with any SQLite browser and click around — no code needed.
