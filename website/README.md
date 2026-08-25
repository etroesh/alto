# `website/` — The Website

**Everything a person sees at alto.troesh.net.** Three files, no build step, no frameworks — the file you read is exactly the file the browser runs.

| File | What it is |
|---|---|
| **`index.html`** | The page structure: the controls on the left, the figures, the table, the chart. |
| **`app.js`** | The behaviour. Asks the API questions and draws the answers. **No modeling happens here** — every number on screen was computed by the Python on the server. |
| **`style.css`** | Every colour and size, organised in the order the page is built. All colours are named at the top; nothing below that block contains a raw colour value. |
| **`methods.html`** | The Methods page: the formulation, the data story, the bugs, the validation, the limitations. This is the page that makes the project defensible. |
| **`data/baseline_index.json`** | Every day of 2023 pre-solved — 67 KB. The year chart draws from this the instant the page loads, so the site works even if the solver is slow to wake. Regenerate with `python scripts/precompute_baselines.py`. |

## The two data colours

The Gantt chart uses exactly two signal colours: **teal** for an aircraft that moved to a different gate, **orange** for one running late. Both were checked with a colour-vision validator against the exact page background, in light and dark mode separately — they stay distinguishable for the common forms of colour blindness and clear 3:1 contrast. Ordinary unchanged aircraft are deliberately grey, so the things that changed are the things you see.

## Changing it

Edit a file, commit, push, pull on the server. **No restart needed** — static files take effect immediately. Only Python changes need `sudo systemctl restart alto-api`.
