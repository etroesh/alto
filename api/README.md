# `api/` — The Server

**The bridge between the engine and the website.** When someone moves a slider on alto.troesh.net, the request lands here, the engine runs, and the answer goes back as JSON.

`main.py` contains **no modeling logic of its own**, on purpose. It calls the same modules in `alto/` that the notebooks call. There is no second implementation that could drift from the one that was validated.

## The endpoints

| Endpoint | What it does | Speed |
|---|---|---|
| `GET /api/health` | Is the service up, is the database there | instant |
| `GET /api/gates` | The 57-gate roster | instant |
| `GET /api/days` | Every date with a one-line summary — powers the date picker | instant |
| `GET /api/day/{date}` | One day: every block, optimally assigned — powers the gate chart | 0.3s cold, 0.02s warm |
| `POST /api/optimize` | A disruption in, damage and recovery out — **the main screen** | ~0.5s |
| `POST /api/assignment` | The recovered gate plan itself, shaped for redrawing the chart | ~0.5s |

`POST` endpoints take `use_exact_solver: true` to run the integer program instead of the network flow — about 14 seconds instead of half a second, for roughly 9% better passenger walking.

## Running it locally

```bash
uvicorn api.main:app --reload
```

Then open **http://127.0.0.1:8000/docs** — FastAPI generates a page where you can try every endpoint in the browser without writing any code. Good place to poke around.

## Deploying it

See [`../deploy/DEPLOY.md`](../deploy/DEPLOY.md).
