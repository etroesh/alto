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
  beforeBlocks: [],      // the day as scheduled, kept so it can be compared
  afterBlocks: [],       // the day after the disruption and the re-plan
  view: "before",        // which of the two the chart is showing
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
  const height = 96;
  const axisRoom = 14;
  const plotHeight = height - axisRoom;

  // Daily turns, not gates used. Gates only range 20 to 36, which flattens the
  // year into a wall of near-identical bars. Turns run 132 to 256 and show the
  // shape of the operation - a summer peak and a winter trough.
  const values = days.map(function (d) { return d.turns; });
  const highest = Math.max.apply(null, values);
  const barWidth = width / days.length;

  let markup = "";

  days.forEach(function (day, index) {
    const barHeight = (day.turns / highest) * plotHeight;
    const isSelected = day.date === state.date;
    markup += '<rect class="day' + (isSelected ? " selected" : "") + '"'
      + ' x="' + (index * barWidth).toFixed(2) + '" y="' + (plotHeight - barHeight).toFixed(2) + '"'
      + ' width="' + Math.max(barWidth - 0.4, 0.4).toFixed(2) + '" height="' + barHeight.toFixed(2) + '"'
      + ' data-date="' + day.date + '" data-turns="' + day.turns
      + '" data-gates="' + day.gates_used + '"></rect>';
  });

  // A seven-day average drawn over the top. Daily traffic swings hard by day of
  // week - Thursday runs 215 turns, Saturday 188 - and that weekly sawtooth
  // hides the seasonal trend underneath it. Averaging over exactly one week
  // cancels the day-of-week effect and leaves the season.
  const WINDOW = 7;
  let path = "";
  for (let index = 0; index < days.length; index++) {
    const from = Math.max(0, index - Math.floor(WINDOW / 2));
    const to = Math.min(days.length, from + WINDOW);
    let total = 0;
    for (let k = from; k < to; k++) total += days[k].turns;
    const average = total / (to - from);

    const x = index * barWidth + barWidth / 2;
    const y = plotHeight - (average / highest) * plotHeight;
    path += (index === 0 ? "M" : "L") + x.toFixed(1) + " " + y.toFixed(1);
  }
  markup += '<path class="trend" d="' + path + '"></path>';

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
      showTooltip(event, "<b>" + rect.dataset.date + "</b><br>"
        + rect.dataset.turns + " turns · " + rect.dataset.gates + " gates");
    });
    rect.addEventListener("mouseleave", hideTooltip);
  });
}

/* ---- what makes a day worth looking at ---------------------------------- */
/* A date picker with 365 equally plausible options gives a visitor nothing to
   go on. These shortcuts and the facts panel answer "why this day?" before
   they have to ask. */

const HOLIDAYS = {
  "2023-01-01": "New Year's Day",
  "2023-05-29": "Memorial Day",
  "2023-07-04": "Independence Day",
  "2023-09-04": "Labor Day",
  "2023-11-23": "Thanksgiving Day",
  "2023-12-24": "Christmas Eve",
  "2023-12-25": "Christmas Day",
  "2023-12-31": "New Year's Eve",
};

function ordinal(n) {
  // "103rd busiest" reads; "#103 of 365" makes you work it out.
  const lastTwo = n % 100;
  if (lastTwo >= 11 && lastTwo <= 13) return n + "th";
  const last = n % 10;
  if (last === 1) return n + "st";
  if (last === 2) return n + "nd";
  if (last === 3) return n + "rd";
  return n + "th";
}


function extremeDay(days, field, biggest) {
  return days.reduce(function (best, day) {
    if (!best) return day;
    return (biggest ? day[field] > best[field] : day[field] < best[field]) ? day : best;
  }, null);
}

function drawQuickPicks() {
  const days = state.yearDays;
  if (days.length === 0) return;

  const picks = [
    ["Busiest", extremeDay(days, "turns", true).date],
    ["Quietest", extremeDay(days, "turns", false).date],
    ["Most gates", extremeDay(days, "gates_used", true).date],
    ["Highest fees", extremeDay(days, "total_cost", true).date],
  ];

  document.getElementById("quick-picks").innerHTML = picks.map(function (pick) {
    return '<button data-date="' + pick[1] + '">' + pick[0] + "</button>";
  }).join("");

  document.querySelectorAll("#quick-picks button").forEach(function (button) {
    button.addEventListener("click", function () {
      document.getElementById("date-input").value = button.dataset.date;
      state.date = button.dataset.date;
      loadDay();
    });
  });
}

function drawDayFacts() {
  const panel = document.getElementById("day-facts");
  const days = state.yearDays;
  const day = days.filter(function (d) { return d.date === state.date; })[0];
  if (!day) { panel.innerHTML = ""; return; }

  // Rank is the fastest way to say whether a number is unusual.
  const busierThan = days.filter(function (d) { return d.turns > day.turns; }).length;
  const costlierThan = days.filter(function (d) { return d.total_cost > day.total_cost; }).length;
  const medianTurns = days.map(function (d) { return d.turns; })
    .sort(function (a, b) { return a - b; })[Math.floor(days.length / 2)];

  const weekday = new Date(day.date + "T12:00:00")
    .toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" });

  let note = "";
  if (HOLIDAYS[day.date]) note = HOLIDAYS[day.date] + ".";
  // The top ten days sit within 1.5% of each other, so "the busiest day" is a
  // near-tie decided by a handful of aircraft. Saying so is more honest than
  // presenting one date as a fact - and it survives the data being improved.
  if (busierThan === 0) note = "The busiest day of 2023 by aircraft visits - though the top ten days are within 1.5% of each other, all in late July and August.";
  else if (busierThan < 10) note = (note ? note + " " : "") + "One of the ten busiest days of the year, all of which fall in late July and August and sit within 1.5% of each other.";
  if (busierThan === days.length - 1) note = (note ? note + " " : "") + "The quietest day of the year — Thanksgiving Day is famously dead in the air; it is the days on either side that are busy.";
  if (costlierThan === 0) note = (note ? note + " " : "") + "The highest fees of any day in 2023.";
  if (day.gates_used > 57) {
    note = (note ? note + " " : "")
      + "Needs more than Alaska's 57 preferential gates — the overflow is billed per turn as common-use.";
  }

  panel.innerHTML =
    '<div class="headline">' + weekday + "</div>"
    + '<div class="row"><span>Aircraft turns</span><b>' + day.turns + "</b></div>"
    + '<div class="row"><span></span><b style="color:var(--muted);font-weight:400">'
      + ordinal(busierThan + 1) + " busiest day of the year</b></div>"
    + '<div class="row"><span>Stands needed</span><b>' + day.gates_used
      + (day.gates_used > 57 ? " — over its 57" : " of 57") + "</b></div>"
    + '<div class="row"><span>Turns per stand</span><b>' + day.turns_per_gate + "</b></div>"
    // NOT "cost to run". This is towing, parking and common-use fees only -
    // it excludes fuel, crew, landing fees, rent and everything else an
    // airline actually spends. A label implying otherwise would be a lie by
    // omission on the most-read part of the page.
    + '<div class="row"><span>Parking &amp; gate fees</span><b>' + shortMoney(day.total_cost) + "</b></div>"
    + '<div class="row"><span></span><b style="color:var(--muted);font-weight:400">'
      + ordinal(costlierThan + 1) + " highest of the year</b></div>"
    + '<div class="row"><span>vs median day</span><b>'
      + (day.turns >= medianTurns ? "+" : "") + (day.turns - medianTurns) + " turns</b></div>"
    + (note ? '<div class="tag-note">' + note + "</div>" : "");
}

/* ---- 4. THE GANTT CHART ------------------------------------------------- */
/* One row per gate, time running left to right, a bar for every stretch of
   time an aircraft occupies that gate. This is the picture of the answer. */

const ROW_HEIGHT = 15;
const LABEL_WIDTH = 42;
const TOP_MARGIN = 20;

// An aircraft that ends its day in Seattle has no onward destination, and one
// that starts its day here has no origin. Writing "? -> SEA -> ?" for those
// looked like missing data when it is in fact the whole point of the visit, so
// each case gets its own honest label.
function routeLabel(from, to) {
  if (from && to) return from + " \u2192 SEA \u2192 " + to;
  if (from) return from + " \u2192 SEA  (arrival only)";
  if (to) return "SEA \u2192 " + to + "  (departure only)";
  return "SEA";
}

/* ---- BEFORE AND AFTER ---------------------------------------------------
   The chart answers "what does the day look like?" - but the question people
   actually have is "what did the disruption change?", and that is a
   comparison. Holding both plans and switching between them in place answers
   it far better than either picture on its own: the rows do not move, so what
   moved is what your eye catches. */

function setChartView(view) {
  const hasAfter = state.afterBlocks.length > 0;
  if (view === "after" && !hasAfter) view = "before";
  state.view = view;

  state.blocks = view === "after" ? state.afterBlocks : state.beforeBlocks;
  drawGantt(state.blocks);

  document.querySelectorAll("#chart-view button").forEach(function (button) {
    const isCurrent = button.dataset.view === view;
    button.setAttribute("aria-pressed", isCurrent ? "true" : "false");
    button.disabled = button.dataset.view === "after" && !hasAfter;
  });

  const note = document.getElementById("chart-view-note");
  if (note) {
    note.textContent = !hasAfter
      ? "Run a scenario to compare the two."
      : (view === "before"
        ? "The day as Alaska actually scheduled it."
        : "After the disruption and the re-plan. Blue is an aircraft that changed gate.");
  }
}

function drawGantt(blocks) {
  const svg = document.getElementById("gantt");

  if (!blocks || blocks.length === 0) {
    svg.innerHTML = "";
    return;
  }

  // Which gates are in use, ordered the way the terminal is: C, then the
  // North Satellite, then D, and numerically within each.
  // Gates in use, PLUS any the scenario has closed. A closed gate keeps its
  // row so you can see it is out of service, rather than having to notice a
  // line that is no longer there.
  const gateSet = {};
  blocks.forEach(function (block) { if (block.gate) gateSet[block.gate] = true; });
  state.closedGates.forEach(function (gateId) { gateSet[gateId] = true; });

  const closed = new Set(state.closedGates);
  const gates = Object.keys(gateSet).sort(function (a, b) {
    const order = { C: 0, N: 1, D: 2, S: 3 };
    if (order[a[0]] !== order[b[0]]) return order[a[0]] - order[b[0]];
    return parseInt(a.slice(1), 10) - parseInt(b.slice(1), 10);
  });

  const rowOf = {};
  gates.forEach(function (gate, index) { rowOf[gate] = index; });

  // An aircraft parked away from the terminal is the SAME aircraft, so it gets
  // the same colour under a diagonal hatch rather than a colour of its own.
  // Four separate hues could not be kept far enough apart to stay readable to
  // a colour-blind viewer; a texture costs nothing and never collides.
  const HATCH = '<defs><pattern id="hatch" width="6" height="6"'
    + ' patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
    + '<rect width="6" height="6" fill="var(--block)"></rect>'
    + '<line x1="0" y1="0" x2="0" y2="6" stroke="var(--block-line)" stroke-width="1"></line>'
    + "</pattern>"
    // Moved AND late. A 2px rust outline on a blue bar was the first attempt
    // and it was unreadable at this bar height - at 11 pixels tall the outline
    // is most of the bar. Rust stripes over the blue fill read instantly, and
    // reuse the texture language already established for a split visit.
    + '<pattern id="hatch-late" width="6" height="6"'
    + ' patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
    + '<rect width="6" height="6" fill="var(--moved)"></rect>'
    + '<line x1="0" y1="0" x2="0" y2="6" stroke="var(--delayed)" stroke-width="2.5"></line>'
    + "</pattern></defs>";

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

  let markup = HATCH;

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

  // Gate names down the left, with a faint band on every other row so the eye
  // can follow a line across a chart this wide without losing it.
  gates.forEach(function (gate, index) {
    const rowTop = TOP_MARGIN + index * ROW_HEIGHT;

    if (closed.has(gate)) {
      markup += '<rect class="closed-row" x="' + LABEL_WIDTH + '" y="' + rowTop
        + '" width="' + plotWidth + '" height="' + ROW_HEIGHT + '"></rect>';
    } else if (index % 2 === 1) {
      markup += '<rect class="band" x="' + LABEL_WIDTH + '" y="' + rowTop
        + '" width="' + plotWidth + '" height="' + ROW_HEIGHT + '"></rect>';
    }

    markup += '<text class="gate-label' + (closed.has(gate) ? " closed" : "")
      + '" x="' + (LABEL_WIDTH - 8) + '" y="' + (rowTop + ROW_HEIGHT / 2 + 3)
      + '" text-anchor="end">' + gate + "</text>";
  });

  // The aircraft.
  blocks.forEach(function (block) {
    if (!block.gate) return;
    // Inset by a pixel each side: two adjacent aircraft at one gate then show
    // a 2px gap of paper between them, and the hairline edge stays visible.
    const x = xOf(block.start) + 1;
    const barWidth = Math.max(xOf(block.end) - xOf(block.start) - 2, 2);
    const y = TOP_MARGIN + rowOf[block.gate] * ROW_HEIGHT + 2;

    // The class decides the colour, and the order here is the priority order:
    // a moved aircraft is the thing worth seeing first.
    // Priority order, and the reason for it: a moved aircraft is the thing the
    // model DID, so it wins the fill. Lateness is what you asked for, so when
    // both are true the bar keeps the blue fill and takes a rust outline -
    // one mark, two facts, no fifth colour invented to hold the combination.
    let className = "bar";
    const moved = block.was_at_gate && block.was_at_gate !== block.gate;
    const late = block.injected_delay > 0;
    if (moved && late) className += " moved late";
    else if (moved) className += " moved";
    else if (late) className += " delayed";
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
      + ' data-from="' + (block.from || "") + '"'
      + ' data-to="' + (block.to || "") + '"'
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
        + routeLabel(data.from, data.to) + "<br>"
        + clockFromAbsoluteMinutes(data.start) + " – " + clockFromAbsoluteMinutes(data.end)
        + " (" + (data.end - data.start) + " min)";

      // What the colour is telling you, spelled out. An aircraft can be both
      // moved AND late, and the bar can only be one colour - so the tooltip
      // has to carry both, and name the gate it came from rather than leaving
      // "moved" as a fact with no detail.
      const wasMoved = data.was && data.was !== data.gate;
      const isLate = Number(data.delay) > 0;
      if (wasMoved) {
        text += "<br><b>moved: " + data.was + " \u2192 " + data.gate + "</b>";
      }
      if (isLate) {
        text += "<br><b>" + data.delay + " min late</b>"
          + (wasMoved ? " — and moved because of it" : "");
      }
      // Four kinds of block, and each means something different on the chart.
      if (data.type === "arrival") text += "<br>arrival block, towed after";
      else if (data.type === "departure") text += "<br>brought back to board";
      else if (data.type === "arrival_only") text += "<br>arrival only - no onward flight in this data";
      else if (data.type === "departure_only") text += "<br>departure only - no arrival in this data";
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
      // Not a price. Idle gate time is not billed to an airline, and pricing
      // it made closing gates look like a saving - see docs/decisions.md D42.
      + "<td>" + (priced.gate_utilisation_percent === undefined
          ? "—" : priced.gate_utilisation_percent + "%") + "</td>"
      + "<td>" + money(priced.towing_cost) + "</td>"
      + "<td>" + money(priced.common_use_cost) + "</td>"
      + "<td><b>" + money(priced.total_cost) + "</b></td>"
      + "<td>" + gates + "</td>"
      + "</tr>";
  });
  document.getElementById("breakdown-body").innerHTML = markup;
}

/* ---- THE VERDICT --------------------------------------------------------
   Four figures and a seven-column table are the evidence; this is the answer.
   Someone who does not read tables should still be able to leave the page
   knowing what happened, so every run writes one plain sentence. */

function setVerdict(html, isIdle) {
  const panel = document.getElementById("verdict");
  if (!panel) return;
  panel.innerHTML = html;
  panel.className = isIdle ? "verdict idle" : "verdict";
}

function verdictFor(result) {
  const tails = Object.keys(state.delays);
  const closed = state.closedGates.length;

  // What did you actually do? Say that back first, so the number has a cause.
  let cause;
  if (tails.length === 1) {
    cause = "Delaying <b>" + tails[0] + "</b> by <b>" + state.delays[tails[0]] + " minutes</b>";
  } else if (tails.length > 1) {
    cause = "Delaying <b>" + tails.length + " aircraft</b>";
  } else if (closed > 0) {
    cause = "Closing <b>" + closed + (closed === 1 ? " gate</b>" : " gates</b>");
  } else {
    cause = "This day, exactly as scheduled,";
  }
  if (tails.length > 0 && closed > 0) {
    cause += " and closing <b>" + closed + (closed === 1 ? " gate</b>" : " gates</b>");
  }

  if (result.disruption_cost <= 0) {
    return cause + " adds nothing — the schedule absorbs it without a single "
      + "aircraft waiting for a gate, and no fee changes.";
  }

  // The re-plan does not always beat the improvised plan. Saying so, with the
  // reason, is more useful than a zero with no explanation.
  if (result.recovery_improved === false) {
    return cause + " adds <b>" + money(result.disruption_cost) + "</b> to the day's fees — and "
      + "re-planning found <b>nothing better</b> than improvising. With this many gates shut, "
      + "the fast solver has to keep each aircraft's whole chain of turns on one stand, which "
      + "costs more in common-use fees than placing them one at a time. Tick "
      + "<b>Solve exactly</b> and it usually wins.";
  }

  let text = cause + " adds <b>" + money(result.disruption_cost) + "</b> to the day's fees. "
    + "Re-planning the whole day's gates gets <b class='up'>" + money(result.recovered_dollars)
    + "</b> of it back — " + result.recovered_percent + "% — by moving <b>"
    + result.aircraft_moved_count + "</b> aircraft to different gates";
  text += result.solver === "integer program"
    ? ", solved exactly in " + result.seconds + " seconds."
    : ", in " + result.seconds + " seconds.";
  return text;
}

/* A one-line summary of the scenario as it currently stands, so the button
   never has to be pressed just to find out what is set. */
function describeScenario() {
  const tails = Object.keys(state.delays);
  const closed = state.closedGates.length;
  const parts = [];
  if (tails.length === 1) parts.push("<b>" + tails[0] + "</b> " + state.delays[tails[0]] + " min late");
  else if (tails.length > 1) parts.push("<b>" + tails.length + " aircraft</b> delayed");
  if (closed > 0) parts.push("<b>" + closed + "</b> gate" + (closed === 1 ? "" : "s") + " closed");
  const element = document.getElementById("scenario-now");
  if (!element) return;
  element.innerHTML = parts.length === 0
    ? "Nothing changed yet — running now gives you the day as it was scheduled."
    : parts.join(" · ") + " on " + state.date;
}

function clearFigures() {
  ["fig-disruption", "fig-recovered", "fig-share", "fig-moved"].forEach(function (id) {
    document.getElementById(id).textContent = "—";
  });
  document.getElementById("breakdown-body").innerHTML =
    '<tr><td colspan="7" style="color:var(--muted);text-align:left">Run a scenario to fill this in.</td></tr>';
  setVerdict("Nothing broken yet. Delay an aircraft above, then press Run — it takes about a second.", true);
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
  S: "Common-use (billed per turn)",
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
  ["C", "N", "D", "S"].forEach(function (concourse) {
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
  describeScenario();
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
    drawQuickPicks();
    drawDayFacts();
    const turns = state.yearDays.map(function (d) { return d.turns; });
    setStatus("year-status",
      "Between " + Math.min.apply(null, turns) + " and " + Math.max.apply(null, turns)
      + " aircraft visits a day across 2023.");
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
    state.beforeBlocks = day.blocks;
    state.afterBlocks = [];
    setChartView("before");

    // Fill the aircraft picker, in the order the aircraft arrive, and label
    // each with where it came from and where it goes next. A bare list of tail
    // numbers gives you no reason to pick one over another.
    const seen = {};
    const options = [];
    day.blocks.forEach(function (block) {
      if (seen[block.tail]) return;
      seen[block.tail] = true;
      const route = routeLabel(block.from, block.to);
      options.push({
        tail: block.tail,
        label: clockFromAbsoluteMinutes(block.start) + "  " + route + "  ·  " + block.tail,
      });
    });
    const select = document.getElementById("tail-select");
    select.innerHTML = options.map(function (option) {
      return '<option value="' + option.tail + '">' + option.label + "</option>";
    }).join("");

    setStatus("chart-status",
      day.blocks.length + " gate blocks · " + day.gates_used + " gates used of "
      + day.gates_available + " available · the theoretical minimum for this day is "
      + day.minimum_possible_gates + scrollHint());
    document.getElementById("chart-title").textContent = "Gate occupancy · " + state.date;
    drawYearStrip(state.yearDays);
    drawDayFacts();
    announce("Loaded " + state.date + ", " + day.gates_used + " gates used.");
  } catch (error) {
    setStatus("chart-status", "Could not load that day: " + error.message, true);
  }
}

async function runScenario() {
  const button = document.getElementById("run");
  button.disabled = true;
  button.textContent = "Solving…";
  setVerdict("Working — re-solving the whole day's gate plan…", true);
  startClock(state.useExactSolver);
  setStatus("chart-status", "Running the scenario…");

  try {
    // One request returns both the money and the picture. It used to be two,
    // which meant the server solved the same day twice - barely noticeable with
    // the network flow, and twenty wasted seconds with the exact solver.
    const result = await postJson("/api/optimize", currentScenario());

    showFigures(result);
    setVerdict(verdictFor(result), false);
    state.afterBlocks = result.blocks;
    setChartView("after");

    // Bring the answer to the reader rather than leaving four numbers to change
    // quietly somewhere below the fold.
    const results = document.getElementById("results");
    if (results) results.scrollIntoView({ behavior: "smooth", block: "start" });

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
    setVerdict("That scenario could not be solved: " + error.message, true);
    failClock("Could not solve that scenario. See the message under the chart.");
  } finally {
    button.disabled = false;
    button.textContent = "Run again";
  }
}

function renderDelayList() {
  const list = document.getElementById("delay-list");
  const tails = Object.keys(state.delays);
  describeScenario();
  if (tails.length === 0) { list.innerHTML = ""; return; }

  list.innerHTML = tails.map(function (tail) {
    const block = state.blocks.filter(function (b) { return b.tail === tail; })[0];
    const route = block ? routeLabel(block.from, block.to) : "";
    return '<div class="delay-row"><span class="tail">' + tail + "</span>"
      + '<span style="color:var(--muted)">' + route + "</span>"
      + '<span class="mins">+' + state.delays[tail] + "</span>"
      + '<button class="tiny" data-remove="' + tail + '">&times;</button></div>';
  }).join("");

  list.querySelectorAll("button[data-remove]").forEach(function (button) {
    button.addEventListener("click", function () {
      delete state.delays[button.dataset.remove];
      renderDelayList();
    });
  });
}

function wireInfoButtons() {
  // Reuses the chart tooltip so there is one popover in the page, not two.
  document.querySelectorAll("button.info").forEach(function (button) {
    function show(event) { showTooltip(event, button.dataset.info); }
    button.addEventListener("mouseenter", show);
    button.addEventListener("mousemove", show);
    button.addEventListener("focus", function () {
      const box = button.getBoundingClientRect();
      showTooltip({ clientX: box.left, clientY: box.bottom }, button.dataset.info);
    });
    button.addEventListener("mouseleave", hideTooltip);
    button.addEventListener("blur", hideTooltip);
    button.addEventListener("click", function (event) { show(event); });
  });
}


function wireControls() {
  document.getElementById("date-input").addEventListener("change", function (event) {
    state.date = event.target.value;
    loadDay();
  });

  document.querySelectorAll("#chart-view button").forEach(function (button) {
    button.addEventListener("click", function () { setChartView(button.dataset.view); });
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
wireInfoButtons();
loadGates();
loadYear();
loadDay();
