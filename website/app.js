/* ============================================================================
   ALTO - the browser side.

   HOW THIS FILE FITS
   ------------------
     index.html  ->  [ THIS FILE ]  ->  /api/day        draw an undisrupted day
                                    ->  /api/optimize   what did it cost
                                    ->  /api/assignment where did aircraft move

   No modeling happens here. Every number on screen was computed by the Python
   on the server. This file asks questions and draws answers, nothing more.

   Written as plain JavaScript with no libraries and no build step, so the file
   you read is exactly the file the browser runs.

   Sections, in the order they appear:
     1. state and helpers
     2. talking to the API
     3. the year strip
     4. the Gantt chart
     5. the figures and the breakdown table
     6. wiring up the controls
   ========================================================================= */

/* ---- 1. STATE AND HELPERS ---------------------------------------------- */

// Everything the page currently knows. One object, so it is always obvious
// where a value came from.
const state = {
  date: "2023-07-15",
  delays: {},            // tail number -> minutes late
  closedGates: [],       // individual gate ids that are shut
  gates: [],             // the roster, loaded once from the API
  costOverrides: {},
  useExactSolver: false,
  blocks: [],            // what the chart is drawing right now
  yearDays: [],
};

const MINUTES_PER_DAY = 1440;

function money(value) {
  if (value === null || value === undefined) return "—";
  const rounded = Math.round(value);
  return "$" + rounded.toLocaleString("en-US");
}

function shortMoney(value) {
  // Big figures on a small tile read better abbreviated.
  const size = Math.abs(value);
  if (size >= 1000000) return "$" + (value / 1000000).toFixed(1) + "M";
  if (size >= 1000)    return "$" + Math.round(value / 1000) + "k";
  return money(value);
}

function clockFromAbsoluteMinutes(absolute) {
  // The API works in minutes since 1 January. People do not.
  const minuteOfDay = ((absolute % MINUTES_PER_DAY) + MINUTES_PER_DAY) % MINUTES_PER_DAY;
  const hours = Math.floor(minuteOfDay / 60);
  const minutes = minuteOfDay % 60;
  return String(hours).padStart(2, "0") + ":" + String(minutes).padStart(2, "0");
}

function announce(message) {
  // Screen readers are told what changed; sighted users see the status line.
  document.getElementById("live-region").textContent = message;
}

function setStatus(elementId, message, isError) {
  const element = document.getElementById(elementId);
  element.textContent = message;
  element.classList.toggle("error", Boolean(isError));
}

/* ---- 2. TALKING TO THE API ---------------------------------------------- */

async function getJson(path) {
  const response = await fetch(path);
  if (!response.ok) {
    const body = await response.json().catch(function () { return {}; });
    throw new Error(body.detail ? JSON.stringify(body.detail) : response.statusText);
  }
  return response.json();
}

async function postJson(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const body = await response.json().catch(function () { return {}; });
    throw new Error(body.detail ? JSON.stringify(body.detail) : response.statusText);
  }
  return response.json();
}

function currentScenario() {
  return {
    date: state.date,
    delays: state.delays,
    closed_gates: state.closedGates,
    closed_concourses: [],
    cost_overrides: state.costOverrides,
    use_exact_solver: state.useExactSolver,
  };
}

/* ---- 3. THE YEAR STRIP -------------------------------------------------- */
/* 365 thin bars, one per day, height showing how many gates that day needed.
   It is a single measure over time, so it is one colour - the accent - with
   the selected day outlined rather than recoloured. Click any bar to load it. */

function drawYearStrip(days) {
  const svg = document.getElementById("year-strip");
  const width = svg.clientWidth || 900;
  const height = 62;
  const axisRoom = 14;
  const plotHeight = height - axisRoom;

  const maxGates = Math.max.apply(null, days.map(function (d) { return d.gates_used || 0; }));
  const barWidth = width / days.length;

  let markup = "";

  days.forEach(function (day, index) {
    const barHeight = ((day.gates_used || 0) / maxGates) * plotHeight;
    const x = index * barWidth;
    const y = plotHeight - barHeight;
    const isSelected = day.date === state.date;
    markup += '<rect class="day' + (isSelected ? " selected" : "") + '"'
      + ' x="' + x.toFixed(2) + '" y="' + y.toFixed(2) + '"'
      + ' width="' + Math.max(barWidth - 0.5, 0.5).toFixed(2) + '" height="' + barHeight.toFixed(2) + '"'
      + ' fill="var(--accent)" data-date="' + day.date + '"'
      + ' data-gates="' + day.gates_used + '" data-turns="' + day.turns + '"></rect>';
  });

  // A label every month, placed at that month's first day.
  const monthNames = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  days.forEach(function (day, index) {
    if (day.date.slice(8) !== "01") return;
    const monthIndex = parseInt(day.date.slice(5, 7), 10) - 1;
    markup += '<text x="' + (index * barWidth).toFixed(2) + '" y="' + (height - 2) + '">'
      + monthNames[monthIndex] + "</text>";
  });

  svg.setAttribute("viewBox", "0 0 " + width + " " + height);
  svg.innerHTML = markup;

  svg.querySelectorAll("rect.day").forEach(function (rect) {
    rect.addEventListener("click", function () {
      document.getElementById("date-input").value = rect.dataset.date;
      state.date = rect.dataset.date;
      loadDay();
    });
    rect.addEventListener("mousemove", function (event) {
      showTooltip(event,
        "<b>" + rect.dataset.date + "</b><br>"
        + rect.dataset.gates + " gates · " + rect.dataset.turns + " turns");
    });
    rect.addEventListener("mouseleave", hideTooltip);
  });
}

/* ---- 4. THE GANTT CHART ------------------------------------------------- */
/* One row per gate, time running left to right, a bar for every stretch of
   time an aircraft occupies that gate. This is the picture of the answer. */

const ROW_HEIGHT = 15;
const LABEL_WIDTH = 42;
const TOP_MARGIN = 20;

function drawGantt(blocks) {
  const svg = document.getElementById("gantt");

  if (!blocks || blocks.length === 0) {
    svg.innerHTML = "";
    return;
  }

  // Which gates are in use, ordered the way the terminal is: C, then the
  // North Satellite, then D, and numerically within each.
  const gateSet = {};
  blocks.forEach(function (block) { if (block.gate) gateSet[block.gate] = true; });
  const gates = Object.keys(gateSet).sort(function (a, b) {
    const order = { C: 0, N: 1, D: 2 };
    if (order[a[0]] !== order[b[0]]) return order[a[0]] - order[b[0]];
    return parseInt(a.slice(1), 10) - parseInt(b.slice(1), 10);
  });

  const rowOf = {};
  gates.forEach(function (gate, index) { rowOf[gate] = index; });

  // The time window, rounded out to whole hours so the grid lines land neatly.
  let earliest = Infinity;
  let latest = -Infinity;
  blocks.forEach(function (block) {
    if (block.start < earliest) earliest = block.start;
    if (block.end > latest) latest = block.end;
  });
  earliest = Math.floor(earliest / 60) * 60;
  latest = Math.ceil(latest / 60) * 60;

  const plotWidth = Math.max(900, (latest - earliest) * 0.62);
  const width = LABEL_WIDTH + plotWidth + 12;
  const height = TOP_MARGIN + gates.length * ROW_HEIGHT + 8;

  function xOf(minute) {
    return LABEL_WIDTH + ((minute - earliest) / (latest - earliest)) * plotWidth;
  }

  let markup = "";

  // Hour grid and the time axis along the top.
  //
  // A day's chart usually runs past midnight, because aircraft that stay
  // overnight are still occupying gates the next morning. Without marking it,
  // the axis shows 00:00 twice and quietly implies the schedule loops back on
  // itself. So midnight gets a heavier line and a label saying which day the
  // hours to its right belong to.
  for (let minute = earliest; minute <= latest; minute += 60) {
    const x = xOf(minute);
    const isMidnight = ((minute % MINUTES_PER_DAY) + MINUTES_PER_DAY) % MINUTES_PER_DAY === 0;

    markup += '<line class="grid-line' + (isMidnight ? " day-break" : "") + '"'
      + ' x1="' + x.toFixed(1) + '" y1="' + TOP_MARGIN
      + '" x2="' + x.toFixed(1) + '" y2="' + (height - 8) + '"></line>';

    // Every other hour when the chart is long, so labels never collide.
    const labelEveryOtherHour = (latest - earliest) > 1200;
    const hourIndex = Math.round((minute - earliest) / 60);
    if (!labelEveryOtherHour || hourIndex % 2 === 0 || isMidnight) {
      markup += '<text class="axis-text" x="' + x.toFixed(1) + '" y="12" text-anchor="middle">'
        + clockFromAbsoluteMinutes(minute) + "</text>";
    }

    if (isMidnight && minute > earliest) {
      markup += '<text class="axis-text day-break-label" x="' + (x + 5).toFixed(1)
        + '" y="' + (height - 1) + '">next day &#8594;</text>';
    }
  }

  // Gate names down the left.
  gates.forEach(function (gate, index) {
    const y = TOP_MARGIN + index * ROW_HEIGHT + ROW_HEIGHT / 2 + 3;
    markup += '<text class="gate-label" x="' + (LABEL_WIDTH - 8) + '" y="' + y
      + '" text-anchor="end">' + gate + "</text>";
  });

  // The aircraft.
  blocks.forEach(function (block) {
    if (!block.gate) return;
    const x = xOf(block.start);
    const barWidth = Math.max(xOf(block.end) - x, 2);
    const y = TOP_MARGIN + rowOf[block.gate] * ROW_HEIGHT + 2;

    // The class decides the colour, and the order here is the priority order:
    // a moved aircraft is the thing worth seeing first.
    let className = "bar";
    if (block.was_at_gate && block.was_at_gate !== block.gate) className += " moved";
    else if (block.injected_delay > 0) className += " delayed";
    else if (block.type === "arrival" || block.type === "departure") className += " tow";

    markup += '<rect class="' + className + '"'
      + ' x="' + x.toFixed(1) + '" y="' + y + '"'
      + ' width="' + barWidth.toFixed(1) + '" height="' + (ROW_HEIGHT - 4) + '"'
      + ' data-tail="' + block.tail + '"'
      + ' data-gate="' + block.gate + '"'
      + ' data-was="' + (block.was_at_gate || "") + '"'
      + ' data-start="' + block.start + '"'
      + ' data-end="' + block.end + '"'
      + ' data-type="' + block.type + '"'
      + ' data-delay="' + (block.injected_delay || 0) + '"></rect>';
  });

  svg.setAttribute("viewBox", "0 0 " + width + " " + height);
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);
  svg.innerHTML = markup;

  svg.querySelectorAll("rect.bar").forEach(function (rect) {
    rect.addEventListener("mousemove", function (event) {
      const data = rect.dataset;
      let text = "<b>" + data.tail + "</b> · gate " + data.gate + "<br>"
        + clockFromAbsoluteMinutes(data.start) + " – " + clockFromAbsoluteMinutes(data.end)
        + " (" + (data.end - data.start) + " min)";
      if (data.type !== "full") {
        text += "<br>" + (data.type === "arrival" ? "arrival block, towed after" : "brought back to board");
      }
      if (data.was && data.was !== data.gate) text += "<br>moved from " + data.was;
      if (Number(data.delay) > 0) text += "<br>" + data.delay + " min late";
      showTooltip(event, text);
    });
    rect.addEventListener("mouseleave", hideTooltip);
  });

  return gates.length;
}

/* ---- the tooltip -------------------------------------------------------- */

function showTooltip(event, html) {
  const tooltip = document.getElementById("tooltip");
  tooltip.innerHTML = html;
  tooltip.classList.remove("hidden");
  // Keep it on screen near the right edge.
  const offset = 14;
  const wouldOverflow = event.clientX + 280 > window.innerWidth;
  tooltip.style.left = (wouldOverflow ? event.clientX - 270 : event.clientX + offset) + "px";
  tooltip.style.top = (event.clientY + offset) + "px";
}

function hideTooltip() {
  document.getElementById("tooltip").classList.add("hidden");
}

function scrollHint() {
  // A whole day at SEA is wider than any screen, so the chart scrolls inside
  // its own panel. A sideways scrollbar buried in a box is easy to miss, so
  // say it in words - but only when there is actually more to see.
  const wrap = document.querySelector(".chart-wrap");
  if (!wrap) return "";
  return wrap.scrollWidth > wrap.clientWidth + 4
    ? " · scroll the chart sideways to see the rest of the day"
    : "";
}

/* ---- 5. THE FIGURES AND THE BREAKDOWN ----------------------------------- */

function showFigures(result) {
  document.getElementById("fig-disruption").textContent = shortMoney(result.disruption_cost);
  document.getElementById("fig-recovered").textContent = shortMoney(result.recovered_dollars);
  document.getElementById("fig-share").textContent = result.recovered_percent + "%";
  document.getElementById("fig-moved").textContent = result.aircraft_moved_count;

  const rows = [
    ["Normal day", result.baseline, result.gates_used_baseline],
    ["Disrupted, plan unchanged", result.damage, result.gates_used_baseline],
    ["Disrupted, re-optimized", result.recovery, result.gates_used_recovery],
  ];

  let markup = "";
  rows.forEach(function (row) {
    const label = row[0];
    const priced = row[1];
    const gates = row[2];
    markup += "<tr>"
      + "<td>" + label + "</td>"
      + "<td>" + money(priced.delay_cost) + "</td>"
      + "<td>" + money(priced.idle_cost) + "</td>"
      + "<td>" + money(priced.towing_cost) + "</td>"
      + "<td><b>" + money(priced.total_cost) + "</b></td>"
      + "<td>" + gates + "</td>"
      + "</tr>";
  });
  document.getElementById("breakdown-body").innerHTML = markup;
}

function clearFigures() {
  ["fig-disruption", "fig-recovered", "fig-share", "fig-moved"].forEach(function (id) {
    document.getElementById(id).textContent = "—";
  });
  document.getElementById("breakdown-body").innerHTML =
    '<tr><td colspan="6" style="color:var(--muted);text-align:left">Run a scenario to fill this in.</td></tr>';
}

/* ---- 6. THE GATE GRID --------------------------------------------------- */
/* Every gate is its own button. Whole concourses were the first version and
   they were too blunt - real closures are a stand or two for maintenance far
   more often than an entire concourse. The per-concourse "close all" link is
   kept as a shortcut for the dramatic case. */

const CONCOURSE_NAMES = {
  C: "Concourse C",
  N: "North Satellite",
  D: "Concourse D",
};

async function loadGates() {
  const payload = await getJson("/api/gates");
  state.gates = payload.gates;
  drawGateGrid();
}

function drawGateGrid() {
  const container = document.getElementById("gate-groups");
  const closed = new Set(state.closedGates);

  let markup = "";
  ["C", "N", "D"].forEach(function (concourse) {
    const inConcourse = state.gates.filter(function (g) { return g.concourse === concourse; });
    if (inConcourse.length === 0) return;

    const allClosed = inConcourse.every(function (g) { return closed.has(g.gate_id); });

    markup += '<div class="gate-group">'
      + '<div class="gate-group-head">'
      + '<span class="name">' + CONCOURSE_NAMES[concourse] + "</span>"
      + '<button class="close-all" data-concourse="' + concourse + '">'
      + (allClosed ? "reopen all" : "close all " + inConcourse.length)
      + "</button></div><div class=\"chips\">";

    inConcourse.forEach(function (gate) {
      const isClosed = closed.has(gate.gate_id);
      markup += '<button class="chip gate" data-gate="' + gate.gate_id + '"'
        + ' aria-pressed="' + isClosed + '"'
        + ' title="' + gate.gate_id + (isClosed ? " — closed" : " — open") + '">'
        + gate.gate_id + "</button>";
    });

    markup += "</div></div>";
  });

  container.innerHTML = markup;

  container.querySelectorAll(".chip.gate").forEach(function (chip) {
    chip.addEventListener("click", function () {
      const gateId = chip.dataset.gate;
      state.closedGates = state.closedGates.indexOf(gateId) === -1
        ? state.closedGates.concat([gateId])
        : state.closedGates.filter(function (g) { return g !== gateId; });
      drawGateGrid();
    });
  });

  container.querySelectorAll(".close-all").forEach(function (link) {
    link.addEventListener("click", function () {
      const concourse = link.dataset.concourse;
      const ids = state.gates
        .filter(function (g) { return g.concourse === concourse; })
        .map(function (g) { return g.gate_id; });
      const allClosed = ids.every(function (id) { return state.closedGates.indexOf(id) !== -1; });

      state.closedGates = allClosed
        ? state.closedGates.filter(function (id) { return ids.indexOf(id) === -1; })
        : state.closedGates.concat(ids.filter(function (id) { return state.closedGates.indexOf(id) === -1; }));
      drawGateGrid();
    });
  });

  const count = state.closedGates.length;
  document.getElementById("closed-count").textContent =
    count === 0 ? "" : "· " + count + " closed of " + state.gates.length;
}

/* ---- 7. THE SOLVING CLOCK ----------------------------------------------- */
/* The exact solver can take fifteen seconds or more. Without a visible clock
   that reads as a frozen page, and people start clicking things. So: count up
   in real time, say plainly not to touch anything, and leave the final time on
   screen afterwards - it is a genuinely interesting number. */

let clockTimer = null;

function startClock(isExactSolver) {
  const panel = document.getElementById("solving");
  const clock = document.getElementById("solving-clock");
  const note = document.getElementById("solving-note");
  const startedAt = Date.now();

  panel.classList.remove("hidden", "done");
  // Honest numbers, measured on the server: the network flow answers in about
  // a second. The integer program takes roughly 20 seconds, or up to 50 the
  // first time you use it on a new day, because the undisrupted day has to be
  // solved exactly too before there is anything to compare against.
  note.textContent = isExactSolver
    ? "Running the integer program. Around 20 seconds — up to 50 the first time on a new day. Leave the controls alone until it finishes."
    : "Working — this takes about a second. Leave the controls alone.";

  clock.textContent = "0.0s";
  clearInterval(clockTimer);
  clockTimer = setInterval(function () {
    clock.textContent = ((Date.now() - startedAt) / 1000).toFixed(1) + "s";
  }, 100);
}

function stopClock(serverSeconds, solverName) {
  clearInterval(clockTimer);
  const panel = document.getElementById("solving");
  panel.classList.add("done");
  document.getElementById("solving-clock").textContent = serverSeconds + "s";
  document.getElementById("solving-note").textContent =
    "Solved by the " + solverName + ". Safe to change things again.";
}

function failClock(message) {
  clearInterval(clockTimer);
  const panel = document.getElementById("solving");
  panel.classList.add("done");
  document.getElementById("solving-clock").textContent = "—";
  document.getElementById("solving-note").textContent = message;
}

/* ---- 8. LOADING AND WIRING ---------------------------------------------- */

async function loadYear() {
  try {
    const payload = await getJson("data/baseline_index.json");
    state.yearDays = payload.days.filter(function (d) { return d.feasible; });
    drawYearStrip(state.yearDays);
    const gatesUsed = state.yearDays.map(function (d) { return d.gates_used; });
    const lowest = Math.min.apply(null, gatesUsed);
    const highest = Math.max.apply(null, gatesUsed);
    setStatus("year-status",
      state.yearDays.length + " days · Alaska needed between " + lowest + " and " + highest
      + " of its 57 gates · click any day to load it");
  } catch (error) {
    setStatus("year-status", "Could not load the year summary: " + error.message, true);
  }
}

async function loadDay() {
  setStatus("chart-status", "Solving " + state.date + "…");
  clearFigures();
  state.delays = {};
  renderDelayList();

  try {
    const day = await getJson("/api/day/" + state.date);
    state.blocks = day.blocks;
    drawGantt(day.blocks);

    // Fill the aircraft picker with the tails actually flying that day.
    const tails = Array.from(new Set(day.blocks.map(function (b) { return b.tail; }))).sort();
    const select = document.getElementById("tail-select");
    select.innerHTML = tails.map(function (tail) {
      return '<option value="' + tail + '">' + tail + "</option>";
    }).join("");

    setStatus("chart-status",
      day.blocks.length + " gate blocks · " + day.gates_used + " gates used of "
      + day.gates_available + " available · the theoretical minimum for this day is "
      + day.minimum_possible_gates + scrollHint());
    document.getElementById("chart-title").textContent = "Gate occupancy · " + state.date;
    drawYearStrip(state.yearDays);
    announce("Loaded " + state.date + ", " + day.gates_used + " gates used.");
  } catch (error) {
    setStatus("chart-status", "Could not load that day: " + error.message, true);
  }
}

async function runScenario() {
  const button = document.getElementById("run");
  button.disabled = true;
  button.textContent = "Solving…";
  startClock(state.useExactSolver);
  setStatus("chart-status", "Running the scenario…");

  try {
    // One request returns both the money and the picture. It used to be two,
    // which meant the server solved the same day twice - barely noticeable with
    // the network flow, and twenty wasted seconds with the exact solver.
    const result = await postJson("/api/optimize", currentScenario());

    showFigures(result);
    state.blocks = result.blocks;
    drawGantt(result.blocks);

    setStatus("chart-status",
      "Solved with the " + result.solver + " in " + result.seconds + "s · "
      + result.aircraft_moved_count + " aircraft moved · "
      + result.gates_used_recovery + " gates used of " + result.gates_available
      + " available" + scrollHint());
    stopClock(result.seconds, result.solver);
    announce("Scenario solved in " + result.seconds + " seconds. "
      + result.recovered_percent + " percent of the disruption recovered.");
  } catch (error) {
    setStatus("chart-status", "That scenario could not be solved: " + error.message, true);
    failClock("Could not solve that scenario. See the message under the chart.");
  } finally {
    button.disabled = false;
    button.textContent = "Run scenario";
  }
}

function renderDelayList() {
  const list = document.getElementById("delay-list");
  const tails = Object.keys(state.delays);
  if (tails.length === 0) { list.innerHTML = ""; return; }

  list.innerHTML = tails.map(function (tail) {
    return '<div class="delay-row"><span class="tail">' + tail + "</span>"
      + '<span class="mins">+' + state.delays[tail] + " min</span>"
      + '<button class="tiny" data-remove="' + tail + '">remove</button></div>';
  }).join("");

  list.querySelectorAll("button[data-remove]").forEach(function (button) {
    button.addEventListener("click", function () {
      delete state.delays[button.dataset.remove];
      renderDelayList();
    });
  });
}

function wireControls() {
  document.getElementById("date-input").addEventListener("change", function (event) {
    state.date = event.target.value;
    loadDay();
  });

  document.getElementById("add-delay").addEventListener("click", function () {
    const tail = document.getElementById("tail-select").value;
    const minutes = parseInt(document.getElementById("delay-minutes").value, 10);
    if (!tail || !minutes || minutes < 1) return;
    state.delays[tail] = minutes;
    renderDelayList();
  });

  const delayCost = document.getElementById("delay-cost");
  delayCost.addEventListener("input", function () {
    const value = Number(delayCost.value);
    document.getElementById("delay-cost-value").textContent = "$" + value.toFixed(2);
    state.costOverrides.delay_cost_per_minute = value;
  });

  const propagation = document.getElementById("propagation");
  propagation.addEventListener("input", function () {
    const value = Number(propagation.value);
    document.getElementById("propagation-value").textContent = value.toFixed(2);
    state.costOverrides.delay_propagation_factor = value;
  });

  document.getElementById("exact-solver").addEventListener("change", function (event) {
    state.useExactSolver = event.target.checked;
  });

  document.getElementById("run").addEventListener("click", runScenario);

  // Redraw the year strip when the window resizes, since it is width-relative.
  let resizeTimer = null;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      if (state.yearDays.length) drawYearStrip(state.yearDays);
    }, 150);
  });
}

/* ---- go ------------------------------------------------------------------ */

wireControls();
loadGates();
loadYear();
loadDay();
