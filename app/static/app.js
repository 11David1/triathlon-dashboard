"use strict";
const $ = (id) => document.getElementById(id);
const SPORT_NAMES = { swim: "Schwimmen", bike: "Rad", run: "Laufen", brick: "Koppel", strength: "Kraft", rest: "Ruhetag", other: "Sonstiges" };
const SPORT_SHORT = { swim: "Swim", bike: "Rad", run: "Lauf", brick: "Kopp.", strength: "Kraft", rest: "Ruhe", other: "Sonst." };
const STATUS_TEXT = { done: "erledigt", partial: "teilweise", missed: "verpasst", open: "geplant", rest: "Ruhetag", calendar: "laut Kalender" };
const STACK = ["swim", "bike", "run", "strength", "other"];
const MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"];
const DOW = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];

const WEEKLY_SPORTS = [["all", "Alle"], ["swim", "Schwimmen"], ["bike", "Rad"], ["run", "Laufen"]];
const WEEKLY_METRICS = [["time", "Zeit"], ["km", "Distanz"], ["hm", "Höhenmeter"]];
const WEEKLY_RANGES = [[12, "12 W"], [26, "6 M"], [52, "1 J"]];
const weekly = { sport: "all", metric: "time", range: 12, pinned: null };
const state = { today: null, month: null, weekStart: null, currentWeek: null, calendar: null, editing: null, activityId: null, dayDate: null };
const timers = {};

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "on") for (const [ev, fn] of Object.entries(value)) node.addEventListener(ev, fn);
    else node[key] = value;
  }
  for (const child of children) if (child !== null && child !== undefined) node.append(child);
  return node;
}

async function api(path, options = {}) {
  const init = { ...options };
  if (init.body !== undefined) {
    init.body = JSON.stringify(init.body);
    init.headers = { "Content-Type": "application/json" };
  }
  const response = await fetch(path, init);
  if (response.status === 401 && path !== "/api/login") {
    location.href = "/login";
    throw new Error("Nicht angemeldet");
  }
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = data && data.detail;
    throw new Error(typeof detail === "string" ? detail : "Die Eingaben sind ungültig oder der Server hat einen Fehler gemeldet.");
  }
  return data;
}

function showBanner(message, action) {
  const banner = $("banner");
  banner.replaceChildren(message);
  if (action) banner.append(el("button", { textContent: action.label, on: { click: action.run } }));
  banner.hidden = false;
}

// --- Formate ---------------------------------------------------------------
const pad = (n) => String(n).padStart(2, "0");
const iso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const parse = (s) => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
const de = (s) => { const d = parse(s); return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()}`; };
const deShort = (s) => { const d = parse(s); return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.`; };
const dayName = (s) => DOW[(parse(s).getDay() + 6) % 7];
const num = (v, digits = 0) => v.toLocaleString("de-DE", { minimumFractionDigits: digits, maximumFractionDigits: digits });

function hours(seconds) {
  if (!seconds) return "0:00 h";
  const minutes = Math.round(seconds / 60);
  return `${Math.floor(minutes / 60)}:${pad(minutes % 60)} h`;
}

function clock(seconds) {
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h ? `${h}:${pad(m)}:${pad(s % 60)}` : `${m}:${pad(s % 60)}`;
}

function distance(sport, meters) {
  if (!meters) return null;
  return sport === "swim" ? `${num(meters)} m` : `${num(meters / 1000, 1)} km`;
}

function pace(sport, speed) {
  if (!speed) return null;
  if (sport === "bike") return { k: "Tempo", v: `${num(speed * 3.6, 1)} km/h` };
  if (sport === "swim") return { k: "Pace", v: `${clock(100 / speed)} /100 m` };
  if (sport === "run") return { k: "Pace", v: `${clock(1000 / speed)} /km` };
  return { k: "Tempo", v: `${num(speed * 3.6, 1)} km/h` };
}

function activityLabel(a) {
  return a.name || SPORT_NAMES[a.sport];
}

// --- Überblick -------------------------------------------------------------
// WHOOP-Zonen: grün ab 67 %, gelb ab 34 %, darunter rot.
function recoveryZone(value) {
  if (value >= 67) return ["good", "gut erholt"];
  if (value >= 34) return ["medium", "mittel erholt"];
  return ["low", "wenig erholt"];
}

function renderAlerts(alerts) {
  const banner = $("banner");
  banner.hidden = !alerts.length;
  banner.className = alerts.some((a) => a.level !== "info") ? "banner" : "banner info";
  banner.replaceChildren(...alerts.map((alert) => el("div", { class: "alert-row" },
    el("span", { textContent: alert.text }),
    alert.action === "settings" ? el("button", { type: "button", textContent: "Einstellungen", on: { click: openSettings } }) : null)));
}

function greetingWord(hour) {
  if (hour >= 5 && hour < 11) return "Guten Morgen";
  if (hour >= 11 && hour < 14) return "Mahlzeit";
  if (hour >= 14 && hour < 18) return "Guten Tag";
  if (hour >= 18 && hour < 23) return "Guten Abend";
  return "Noch wach";
}

function renderGreeting() {
  const o = state.overview;
  if (!o) return;
  const now = new Date();
  const word = greetingWord(now.getHours());
  const name = (o.athlete_name || "").trim();
  $("greeting").textContent = word === "Noch wach" ? `Noch wach${name ? `, ${name}` : ""}?` : `${word}${name ? `, ${name}` : ""}`;
  const date = now.toLocaleDateString("de-DE", { weekday: "long", day: "numeric", month: "long" });
  $("greeting-sub").textContent = date;
  const days = o.race.days_left;
  const chip = $("race-chip");
  chip.hidden = days < 0;
  chip.title = `${o.race.name}, ${de(o.race.date)}`;
  chip.replaceChildren(...(days > 0 ? [el("b", { textContent: num(days) }), ` ${days === 1 ? "Tag" : "Tage"} bis ${o.race.name}`] : [el("b", { textContent: "Heute" }), ` ist ${o.race.name}`]));
}

function sinceText(seconds) {
  const minutes = Math.round((Date.now() / 1000 - seconds) / 60);
  if (minutes < 1) return "gerade eben";
  if (minutes < 60) return `vor ${minutes} Min.`;
  const time = new Date(seconds * 1000);
  const sameDay = time.toDateString() === new Date().toDateString();
  return sameDay ? `um ${time.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" })}` : time.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" });
}

function renderSyncPill() {
  const o = state.overview;
  const pill = $("btn-sync");
  if (!o || pill.disabled) return;
  const g = o.garmin;
  pill.classList.toggle("problem", Boolean(g.error));
  if (!g.connected) {
    $("sync-status").textContent = "Garmin verbinden";
    pill.title = "Garmin ist noch nicht verbunden. Den Key trägst du in den Einstellungen ein.";
  } else if (g.last_sync_at) {
    $("sync-status").textContent = sinceText(g.last_sync_at);
    pill.title = `Zuletzt abgeglichen: ${new Date(g.last_sync_at * 1000).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" })}. Klicken für einen neuen Abgleich.`;
  } else {
    $("sync-status").textContent = "erster Abgleich läuft";
    pill.title = "Garmin ist verbunden, der erste Abgleich läuft.";
  }
}

// Begrüßung und "vor x Min." bleiben aktuell, auch wenn die Seite lange offen ist.
setInterval(() => { renderGreeting(); renderSyncPill(); }, 60000);

function renderOverview(o) {
  state.today = o.today;
  state.currentWeek = o.week_start;
  document.title = `${o.race.name} · Dashboard`;
  state.overview = o;
  renderGreeting();
  renderSyncPill();
  renderAlerts(o.alerts);

  renderWeekProgress(o);
}

// Eine Karte für die laufende Woche: Gesamtstunden und je Sportart erledigt gegen geplant.
function renderWeekProgress(o) {
  const start = parse(o.week_start);
  $("week-range").textContent = `${deShort(o.week_start)} bis ${deShort(iso(addDays(start, 6)))}`;
  const sports = STACK.filter((s) => o.sports[s] && (o.sports[s].planned_min || o.sports[s].done_min));
  const done = sports.reduce((sum, s) => sum + o.sports[s].done_min, 0);
  const planned = sports.reduce((sum, s) => sum + o.sports[s].planned_min, 0);
  if (!sports.length) {
    $("week-progress").replaceChildren(el("p", { class: "empty", textContent: "Für diese Woche ist noch nichts geplant oder trainiert." }));
    return;
  }
  const ratio = planned ? Math.min(1, done / planned) : 1;
  const meter = el("div", { class: "meter big", role: "img", ariaLabel: planned ? `${Math.round(done / planned * 100)} Prozent des Wochenplans erledigt` : "Kein Plan" }, el("span"));
  meter.firstChild.style.width = `${Math.round(ratio * 100)}%`;
  meter.firstChild.style.background = "var(--accent)";
  const rows = sports.map((sport) => {
    const s = o.sports[sport];
    const bar = el("span", { class: "bar", role: "img", ariaLabel: `${SPORT_NAMES[sport]}: ${hours(s.done_min * 60)}${s.planned_min ? ` von ${hours(s.planned_min * 60)}` : ""}` }, el("i", { class: `sport-${sport}` }));
    bar.firstChild.style.width = `${s.planned_min ? Math.min(100, Math.round(s.done_min / s.planned_min * 100)) : 100}%`;
    return el("div", { class: "sport-row" },
      el("span", { class: "sport-name" }, el("i", { class: `swatch sport-${sport}` }), SPORT_NAMES[sport]),
      bar,
      el("span", { class: "val", textContent: s.planned_min ? `${hours(s.done_min * 60)} / ${hours(s.planned_min * 60)}` : hours(s.done_min * 60) }));
  });
  const open = Math.max(0, planned - done);
  $("week-progress").replaceChildren(
    el("div", { class: "week-total" }, el("span", { class: "v", textContent: hours(done * 60) }),
      el("span", { class: "muted-inline", textContent: planned ? `von ${hours(planned * 60)} geplant` : "trainiert" })),
    meter, ...rows,
    open ? el("p", { class: "week-open", textContent: `Noch ${hours(open * 60)} geplant.` }) : null);
}

// --- Kalender --------------------------------------------------------------
function monthTitle() {
  $("month-title").textContent = `${MONTHS[state.month.getMonth()]} ${state.month.getFullYear()}`;
}

async function loadCalendar() {
  monthTitle();
  const first = state.month;
  const start = addDays(first, -((first.getDay() + 6) % 7));
  const end = addDays(start, 41);
  state.calendar = await api(`/api/calendar?start=${iso(start)}&end=${iso(end)}`);
  renderCalendar(start);
  if ($("day-dialog").open && state.dayDate) renderDayEntries(state.dayDate);
}

const ICONS = {
  swim: "M1 10.5c2-1.8 3.2-1.8 5 0s3 1.8 5 0 2.6-1.6 4-.6M1 13.5c2-1.8 3.2-1.8 5 0s3 1.8 5 0 2.6-1.6 4-.6M4 8l3.5-3 3 2.5M11.5 2.2a1.4 1.4 0 1 0 .01 0",
  bike: "M4 8.5a2.9 2.9 0 1 0 .01 0M12 8.5a2.9 2.9 0 1 0 .01 0M4 11.4 6.8 5.5h3.4L12 11.4M6.8 5.5 8.5 11.4H4M6 3.5h2",
  run: "M10.2 1.4a1.3 1.3 0 1 0 .01 0M9 5 6.2 8.4l3 2.2-1 3.9M6.2 8.4 5 10.8 2.5 11M9 5l2.3 2.6 2.4-.4M9 5 6.3 4.8 4.6 6.6",
  brick: "M9 1.5 3.8 9H8l-1 5.5L12.2 7H8z",
  strength: "M1.5 8h13M4 4.8v6.4M12 4.8v6.4M2 6.4v3.2M14 6.4v3.2",
  rest: "M13.5 10A6 6 0 0 1 6 2.5a6 6 0 1 0 7.5 7.5z",
  other: "M8 3.2a4.8 4.8 0 1 0 .01 0M8 6.6a1.4 1.4 0 1 0 .01 0",
};

function sportIcon(sport) {
  const icon = svgEl("svg", { viewBox: "0 0 16 16", class: "ico", "aria-hidden": "true" });
  svgEl("path", { d: ICONS[sport] || ICONS.other }, icon);
  return icon;
}

function shortFacts(parts) {
  return parts.filter(Boolean).join(" · ");
}

// Zeile unter dem Titel: Die Dauer bleibt immer sichtbar, der Rest entfällt, wenn der Platz fehlt.
function factsLine(main, extra) {
  const line = el("span", { class: "t2", textContent: main || "" });
  if (extra) line.append(el("span", { class: "extra", textContent: `${main ? " · " : ""}${extra}` }));
  return line;
}

function chipFor(item, kind) {
  const sport = item.sport;
  if (kind === "activity") {
    const facts = shortFacts([hours(item.duration_s), distance(sport, item.distance_m)]);
    return el("button", {
      class: `chip done sport-${sport}`, type: "button",
      title: `${activityLabel(item)} · ${facts} · absolviert`,
      on: { click: (e) => { e.stopPropagation(); openActivity(item.id); } },
    }, sportIcon(sport),
    el("span", { class: "chip-body" }, el("span", { class: "t1", textContent: activityLabel(item) }), factsLine(hours(item.duration_s), distance(sport, item.distance_m))),
    el("span", { class: "i", textContent: "✓", ariaHidden: "true" }));
  }
  const label = item.title || SPORT_NAMES[sport];
  const icon = item.status === "missed" ? "✕" : item.status === "partial" ? "◐" : "";
  const duration = sport !== "rest" && item.duration_min ? hours(item.duration_min * 60) : null;
  const intensity = item.status === "calendar" ? "ohne Garmin" : sport !== "rest" ? item.intensity : "";
  return el("button", {
    class: `chip planned ${item.status} sport-${sport}`, type: "button",
    title: `${label} · ${STATUS_TEXT[item.status]}`,
    on: { click: (e) => { e.stopPropagation(); openDay(item.date, item.id); } },
  }, sportIcon(sport),
  el("span", { class: "chip-body" }, el("span", { class: "t1", textContent: label }), duration || intensity ? factsLine(duration, intensity) : null),
  icon ? el("span", { class: "i", textContent: icon, ariaHidden: "true" }) : null);
}

function isoWeek(date) {
  const thursday = addDays(date, 3 - ((date.getDay() + 6) % 7));
  const firstThursday = new Date(thursday.getFullYear(), 0, 4);
  return 1 + Math.round(((thursday - firstThursday) / 86400000 - 3 + ((firstThursday.getDay() + 6) % 7)) / 7);
}

function weekSummary(weekStart) {
  const from = iso(weekStart);
  const to = iso(addDays(weekStart, 6));
  const { planned, activities } = state.calendar;
  const inWeek = (row) => row.date >= from && row.date <= to;
  const done = activities.filter(inWeek).map((a) => ({ sport: a.sport, duration_s: a.duration_s }));
  // Trainings, die nur im Google Kalender stehen, zählen mit. Koppeleinheiten wie im Überblick aufgeteilt.
  for (const plan of planned.filter(inWeek)) {
    if (plan.status !== "calendar") continue;
    const seconds = (plan.duration_min || 0) * 60;
    if (plan.sport === "brick") done.push({ sport: "bike", duration_s: seconds * 3 / 4 }, { sport: "run", duration_s: seconds / 4 });
    else done.push({ sport: plan.sport, duration_s: seconds });
  }
  const doneSeconds = done.reduce((sum, a) => sum + (a.duration_s || 0), 0);
  const plannedMinutes = planned.filter(inWeek).reduce((sum, p) => sum + (p.duration_min || 0), 0);
  const box = el("div", { class: "week-sum" }, el("div", { class: "kw", textContent: `KW ${isoWeek(weekStart)}` }));
  if (doneSeconds) box.append(el("div", { class: "sum", textContent: hours(doneSeconds) }));
  if (plannedMinutes) box.append(el("div", { class: "of", textContent: `${doneSeconds ? "von " : ""}${hours(plannedMinutes * 60)} geplant` }));
  if (doneSeconds) {
    const bar = el("div", { class: "share", role: "img", ariaLabel: "Anteile der Sportarten" });
    for (const sport of STACK) {
      const seconds = done.filter((a) => a.sport === sport).reduce((sum, a) => sum + (a.duration_s || 0), 0);
      if (!seconds) continue;
      const part = el("span", { class: `sport-${sport}`, title: `${SPORT_NAMES[sport]}: ${hours(seconds)}` });
      part.style.flexGrow = String(seconds);
      bar.append(part);
    }
    box.append(bar);
  }
  return box;
}

// Geplante Einheiten lassen sich auf einen anderen Tag ziehen.
function makeDraggable(chip, plan) {
  chip.draggable = true;
  chip.addEventListener("dragstart", (event) => {
    state.dragging = plan;
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", String(plan.id));
    chip.classList.add("dragging");
  });
  chip.addEventListener("dragend", () => {
    state.dragging = null;
    chip.classList.remove("dragging");
    document.querySelectorAll(".drop-target").forEach((node) => node.classList.remove("drop-target"));
  });
}

function makeDropTarget(cell, date) {
  cell.addEventListener("dragover", (event) => {
    if (!state.dragging) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    cell.classList.add("drop-target");
  });
  cell.addEventListener("dragleave", (event) => {
    if (!cell.contains(event.relatedTarget)) cell.classList.remove("drop-target");
  });
  cell.addEventListener("drop", async (event) => {
    event.preventDefault();
    const plan = state.dragging;
    cell.classList.remove("drop-target");
    if (!plan || plan.date === date) return;
    try {
      await api(`/api/planned/${plan.id}/move`, { method: "POST", body: { date } });
      await refreshPlanViews();
    } catch (error) {
      showBanner(error.message);
    }
  });
}

function renderCalendar(start) {
  const nodes = [...DOW.map((d, i) => el("div", { class: `dow${i > 4 ? " weekend" : ""}`, textContent: d })), el("div", { class: "dow week-head", textContent: "Woche" })];
  const { planned, activities } = state.calendar;
  const daysInMonth = new Date(state.month.getFullYear(), state.month.getMonth() + 1, 0).getDate();
  const offset = (state.month.getDay() + 6) % 7;
  const cells = Math.ceil((offset + daysInMonth) / 7) * 7;
  for (let i = 0; i < cells; i++) {
    const date = addDays(start, i);
    const key = iso(date);
    const classes = ["day"];
    if (date.getMonth() !== state.month.getMonth()) classes.push("other");
    if (key === state.today) classes.push("today");
    if (key < state.today) classes.push("past");
    if (i % 7 > 4) classes.push("weekend");
    const cell = el("div", { class: classes.join(" "), on: { click: () => openDay(key) } },
      el("div", { class: "day-top" },
        el("span", { class: "num", textContent: date.getDate() }),
        el("button", { class: "add", type: "button", textContent: "+", ariaLabel: `Training am ${de(key)} eintragen`, on: { click: (e) => { e.stopPropagation(); openDay(key); } } })));
    makeDropTarget(cell, key);
    const dots = el("div", { class: "dots", ariaHidden: "true" });
    for (const plan of planned.filter((p) => p.date === key)) {
      if (plan.status === "done") continue;
      const chip = chipFor(plan, "plan");
      if (!plan.google_uid) makeDraggable(chip, plan);
      cell.append(chip);
      dots.append(el("i", { class: `dot-mini ${plan.status} sport-${plan.sport}` }));
    }
    for (const activity of activities.filter((a) => a.date === key)) {
      cell.append(chipFor(activity, "activity"));
      dots.append(el("i", { class: `dot-mini done sport-${activity.sport}` }));
    }
    cell.append(dots);
    nodes.push(cell);
    if (i % 7 === 6) nodes.push(weekSummary(addDays(date, -6)));
  }
  $("calendar").replaceChildren(...nodes);
}

function renderSportLegend() {
  const items = ["swim", "bike", "run", "brick", "strength", "rest", "other"].map((s) =>
    el("span", { class: `sport-${s}` }, sportIcon(s), SPORT_NAMES[s]));
  items.push(el("span", { class: "legend-sep" }, "✓ mit Garmin-Daten"), el("span", {}, "blass: laut Kalender, ohne Garmin"), el("span", {}, "gestrichelt: geplant"), el("span", {}, "✕ verpasst"));
  $("sport-legend").replaceChildren(...items);
}

// --- Trainer ---------------------------------------------------------------
const SUGGESTIONS = [
  "Plane meine nächste Woche",
  "Wie steht es um meine Form?",
  "Was soll ich heute trainieren?",
  "Wo stehe ich für das Rennen und was fehlt noch?",
];
const chat = { messages: [], signature: "", timer: null };

function proposalCard(message) {
  const items = message.proposal;
  const minutes = items.reduce((sum, item) => sum + (item.duration_min || 0), 0);
  const units = items.filter((item) => item.sport !== "rest").length;
  const card = el("div", { class: "proposal" },
    el("div", { class: "proposal-head" }, el("b", { textContent: "Planvorschlag" }), el("span", { class: "muted", textContent: `${units} Einheiten · ${hours(minutes * 60)}` })));
  for (const item of items) {
    const facts = item.sport === "rest" ? "" : shortFacts([item.duration_min ? hours(item.duration_min * 60) : null, item.distance_km ? `${num(item.distance_km, 1)} km` : null, item.intensity]);
    card.append(el("div", { class: `proposal-row sport-${item.sport}` },
      el("span", { class: "when", textContent: `${dayName(item.date)} ${deShort(item.date)}` }),
      sportIcon(item.sport),
      el("span", { class: "what" }, el("span", { class: "t1", textContent: item.title || SPORT_NAMES[item.sport] }), facts ? el("span", { class: "t2", textContent: facts }) : null, item.notes ? el("span", { class: "t2", textContent: item.notes }) : null)));
  }
  if (message.proposal_status === "applied") {
    card.append(el("p", { class: "proposal-state", textContent: "✓ Im Kalender eingetragen" }));
  } else if (message.proposal_status === "discarded") {
    card.append(el("p", { class: "proposal-state muted", textContent: "Verworfen" }));
  } else {
    const actions = el("div", { class: "actions end" });
    actions.append(el("button", { type: "button", textContent: "Verwerfen", on: { click: () => discardProposal(message.id) } }));
    if (message.conflicts) {
      actions.append(
        el("button", { type: "button", textContent: "Ergänzen", on: { click: () => applyProposal(message.id, false) } }),
        el("button", { type: "button", class: "primary", textContent: `${message.conflicts} ersetzen`, on: { click: () => applyProposal(message.id, true, message.conflicts) } }));
      card.append(el("p", { class: "muted", textContent: `In diesem Zeitraum stehen schon ${message.conflicts} offene Einträge im Kalender. Du kannst sie ersetzen oder den Vorschlag ergänzen.` }));
    } else {
      actions.append(el("button", { type: "button", class: "primary", textContent: "In den Kalender übernehmen", on: { click: () => applyProposal(message.id, false) } }));
    }
    card.append(actions);
  }
  return card;
}

function chatBubble(message) {
  if (message.role === "user") return el("div", { class: "bubble user", textContent: message.content });
  const bubble = el("div", { class: "bubble trainer-msg analysis" });
  if (message.status === "pending" || message.status === "running") {
    bubble.append(el("div", { class: "status" }, el("span", { class: "spin" }), "Dein Trainer schaut sich deine Daten an, das dauert meist unter einer Minute."));
    return bubble;
  }
  if (message.status === "failed") {
    bubble.append(el("p", { class: "error", textContent: `Das hat nicht geklappt: ${message.error || "unbekannter Fehler"}` }),
      el("button", { type: "button", textContent: "Noch einmal versuchen", on: { click: () => retryChat(message.id) } }));
    return bubble;
  }
  bubble.append(...renderMarkdown(message.content));
  if (message.proposal_note) bubble.append(el("p", { class: "muted", textContent: message.proposal_note }));
  if (message.proposal) bubble.append(proposalCard(message));
  return bubble;
}

function renderChat() {
  const log = $("chat-log");
  const atEnd = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  if (!chat.messages.length) {
    log.replaceChildren(el("div", { class: "chat-empty" },
      el("p", { textContent: "Frag mich zu deinem Training, zur Vorbereitung auf das Rennen oder lass dir die nächste Woche planen. Einen Plan schlage ich vor, in den Kalender kommt er erst mit deinem Klick." }),
      ...SUGGESTIONS.map((text) => el("button", { type: "button", class: "suggestion", textContent: text, on: { click: () => sendChat(text) } }))));
    return;
  }
  log.replaceChildren(...chat.messages.map(chatBubble));
  if (atEnd) log.scrollTop = log.scrollHeight;
}

function chatBusy() {
  return chat.messages.some((m) => m.status === "pending" || m.status === "running");
}

async function loadChat({ scroll = false } = {}) {
  clearTimeout(chat.timer);
  try {
    chat.messages = await api("/api/chat");
  } catch (error) {
    $("chat-error").textContent = error.message;
    $("chat-error").hidden = false;
    return;
  }
  const signature = JSON.stringify(chat.messages);
  if (signature !== chat.signature) {
    chat.signature = signature;
    renderChat();
  }
  if (scroll) $("chat-log").scrollTop = $("chat-log").scrollHeight;
  const busy = chatBusy();
  $("chat-send").disabled = busy;
  if (busy) chat.timer = setTimeout(loadChat, 2000);
}

async function chatAction(run) {
  $("chat-error").hidden = true;
  try {
    await run();
  } catch (error) {
    $("chat-error").textContent = error.message;
    $("chat-error").hidden = false;
  }
}

function sendChat(text) {
  const message = text.trim();
  if (!message || chatBusy()) return Promise.resolve();
  return chatAction(async () => {
    await api("/api/chat", { method: "POST", body: { message } });
    $("chat-input").value = "";
    await loadChat({ scroll: true });
  });
}

function retryChat(id) {
  return chatAction(async () => {
    await api(`/api/chat/${id}/retry`, { method: "POST" });
    await loadChat();
  });
}

function applyProposal(id, replace, count) {
  if (replace && !confirm(`${count} offene Einträge im Zeitraum des Vorschlags löschen und durch den Vorschlag ersetzen?`)) return Promise.resolve();
  return chatAction(async () => {
    const result = await api(`/api/chat/${id}/apply`, { method: "POST", body: { replace } });
    const first = parse(result.first_date);
    state.month = new Date(first.getFullYear(), first.getMonth(), 1);
    await Promise.all([loadChat(), refreshAll()]);
  });
}

function discardProposal(id) {
  return chatAction(async () => {
    await api(`/api/chat/${id}/discard`, { method: "POST" });
    await loadChat();
  });
}

// --- Wissen des Trainers ---------------------------------------------------
function knowledgeProblem(message) {
  $("knowledge-error").textContent = message;
  $("knowledge-error").hidden = !message;
}

async function loadKnowledge() {
  const data = await api("/api/knowledge");
  if (!$("knowledge-dialog").open) $("knowledge-instructions").value = data.instructions;
  const total = data.documents.reduce((sum, doc) => sum + doc.chars, 0);
  $("trainer-knowledge").textContent = data.documents.length || data.instructions ? `Wissen (${data.documents.length + (data.instructions ? 1 : 0)})` : "Wissen";
  $("knowledge-warning").hidden = total <= data.limit;
  $("knowledge-warning").textContent = `Die Unterlagen sind zu lang: Genutzt werden nur die ersten ${num(data.limit)} von ${num(total)} Zeichen. Entferne am besten, was der Trainer nicht braucht.`;
  $("knowledge-list").replaceChildren(...data.documents.map((doc) => el("div", { class: "knowledge-row" },
    el("span", { class: "t1", textContent: doc.title, title: doc.title }),
    el("span", { class: "muted", textContent: `${num(doc.chars)} Zeichen` }),
    el("button", { type: "button", textContent: "Entfernen", ariaLabel: `${doc.title} entfernen`, on: { click: () => removeKnowledge(doc) } }))));
  if (!data.documents.length) $("knowledge-list").replaceChildren(el("p", { class: "empty", textContent: "Noch nichts hinterlegt." }));
}

async function openKnowledge() {
  knowledgeProblem("");
  $("instructions-state").textContent = "";
  await loadKnowledge();
  $("knowledge-dialog").showModal();
}

async function removeKnowledge(doc) {
  if (!confirm(`„${doc.title}“ aus dem Wissen deines Trainers entfernen?`)) return;
  await api(`/api/knowledge/${doc.id}`, { method: "DELETE" });
  await loadKnowledge();
}

async function uploadKnowledge(event) {
  knowledgeProblem("");
  const input = event.target;
  const problems = [];
  for (const file of input.files) {
    const response = await fetch(`/api/knowledge/file?name=${encodeURIComponent(file.name)}`, { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file }).catch(() => null);
    if (!response || !response.ok) {
      const data = response ? await response.json().catch(() => ({})) : {};
      problems.push(response && response.status === 413 ? `${file.name} ist größer als 10 MB.` : data.detail || `${file.name} konnte nicht hochgeladen werden.`);
    } else if ((await response.json()).truncated) {
      problems.push(`${file.name} war sehr lang und wurde gekürzt gespeichert.`);
    }
  }
  input.value = "";
  knowledgeProblem(problems.join(" "));
  await loadKnowledge();
}

// --- Beschwerden ---------------------------------------------------------------
const complaintDrafts = {};
const openComplaints = new Set();
let complaintEditing = null;

function daysSince(date) {
  return Math.round((parse(state.today) - parse(date)) / 86400000);
}

function entryRow(complaint, entry, removable) {
  const head = shortFacts([`${dayName(entry.date)} ${deShort(entry.date)}`, entry.severity !== null ? `Stärke ${entry.severity} von 10` : null]);
  return el("li", { class: "entry-row" },
    el("div", { class: "entry-head" }, el("span", { class: "muted-inline", textContent: head }),
      removable ? el("button", { class: "entry-remove", type: "button", textContent: "×", ariaLabel: `Eintrag vom ${deShort(entry.date)} löschen`, on: { click: () => removeEntry(complaint.id, entry.id) } }) : null),
    el("div", { class: "entry-text", textContent: entry.text }));
}

function activeComplaint(c) {
  const days = daysSince(c.started_on);
  const input = el("textarea", { rows: 2, maxLength: 2000, placeholder: "Wie ist es heute? Zum Beispiel: Freitag besser, morgens noch steif", ariaLabel: `Neuer Eintrag zu ${c.name}`, value: complaintDrafts[c.id] || "", on: { input: (e) => { complaintDrafts[c.id] = e.target.value; } } });
  const severity = el("select", { ariaLabel: "Stärke von 0 bis 10" }, el("option", { value: "", textContent: "Stärke (optional)" }),
    ...Array.from({ length: 11 }, (_, n) => el("option", { value: String(n), textContent: `${n} von 10` })));
  const cause = el("textarea", { rows: 2, maxLength: 2000, value: c.cause, placeholder: "Woher kommt es vermutlich?", on: { change: (e) => saveCause(c, e.target.value, causeState) } });
  const causeState = el("span", { class: "muted-inline cause-state" });
  const form = el("form", { class: "complaint-entry", on: { submit: (e) => { e.preventDefault(); addEntry(c.id, input.value, severity.value); } } },
    input, el("div", { class: "entry-controls" }, severity, el("button", { type: "submit", class: "primary", textContent: "Eintragen" })));
  return el("article", { class: "complaint" },
    el("div", { class: "complaint-head" },
      el("div", {}, el("b", { textContent: c.name }), el("div", { class: "muted-inline small", textContent: `seit ${de(c.started_on)}${days > 0 ? `, ${days} ${days === 1 ? "Tag" : "Tage"}` : ""}` })),
      el("button", { type: "button", textContent: "Bearbeiten", on: { click: () => openComplaint(c) } })),
    form,
    c.entries.length ? el("ul", { class: "entry-list" }, ...c.entries.map((entry) => entryRow(c, entry, true))) : null,
    el("label", { class: "cause-label" }, "Vermutete Ursache ", causeState, cause),
    el("button", { type: "button", class: "resolve", textContent: "✓ Beschwerde ist weg", on: { click: () => setResolved(c.id, true) } }));
}

function pastComplaint(c) {
  return el("details", { class: "complaint past" },
    el("summary", {}, el("b", { textContent: c.name }), el("span", { class: "muted-inline small", textContent: ` ${de(c.started_on)} bis ${de(c.resolved_on)}` })),
    c.cause ? el("p", { class: "small", textContent: `Vermutete Ursache: ${c.cause}` }) : null,
    c.entries.length ? el("ul", { class: "entry-list" }, ...c.entries.map((entry) => entryRow(c, entry, false))) : null,
    el("div", { class: "actions" },
      el("button", { type: "button", textContent: "Wieder aktiv", on: { click: () => setResolved(c.id, false) } }),
      el("button", { type: "button", textContent: "Bearbeiten", on: { click: () => openComplaint(c) } })));
}

async function loadComplaints() {
  const rows = await api("/api/complaints");
  const active = rows.filter((c) => !c.resolved_on);
  const past = rows.filter((c) => c.resolved_on);
  // Aktive Beschwerden stehen als Hinweis bei "Heute", aufklappbar zum Eintragen.
  $("today-complaints").replaceChildren(...active.map((c) => {
    const last = c.entries.find((e) => e.severity !== null);
    const days = daysSince(c.started_on);
    const facts = shortFacts([last ? `${last.severity}/10` : null, days > 0 ? `seit ${days} ${days === 1 ? "Tag" : "Tagen"}` : "seit heute"]);
    const box = el("details", { class: "complaint-hint" },
      el("summary", {}, el("span", { class: "warn-icon", ariaHidden: "true", textContent: "!" }), el("b", { textContent: c.name }), el("span", { textContent: ` · ${facts}` }),
        el("span", { class: "open-label", textContent: "Eintragen" })),
      activeComplaint(c));
    if (openComplaints.has(c.id)) box.open = true;
    box.addEventListener("toggle", () => { if (box.open) openComplaints.add(c.id); else openComplaints.delete(c.id); });
    return box;
  }));
  const nodes = active.map((c) => el("p", { class: "complaint-active" }, el("b", { textContent: c.name }),
    el("span", { class: "muted-inline", textContent: ` · aktiv seit ${de(c.started_on)}, Einträge unter Übersicht → Heute` })));
  if (!active.length) nodes.push(el("p", { class: "empty complaints-empty", textContent: "Gerade keine Beschwerden. Neue meldest du in der Übersicht unter „Heute“. Dein Trainer berücksichtigt alles, was du einträgst." }));
  if (past.length) nodes.push(el("details", { class: "past-list" }, el("summary", { textContent: `Frühere Beschwerden (${past.length})` }), ...past.map(pastComplaint)));
  $("complaints").replaceChildren(...nodes);
}

function complaintAction(run) {
  return run().then(loadComplaints).catch((error) => showBanner(error.message));
}

function addEntry(id, text, severity) {
  if (!text.trim()) return Promise.resolve();
  return complaintAction(async () => {
    await api(`/api/complaints/${id}/entries`, { method: "POST", body: { text, severity: severity === "" ? null : Number(severity) } });
    delete complaintDrafts[id];
  });
}

function removeEntry(complaintId, entryId) {
  if (!confirm("Diesen Eintrag löschen?")) return Promise.resolve();
  return complaintAction(() => api(`/api/complaints/${complaintId}/entries/${entryId}`, { method: "DELETE" }));
}

function setResolved(id, resolved) {
  return complaintAction(() => api(`/api/complaints/${id}/${resolved ? "resolve" : "reopen"}`, { method: "POST" }));
}

async function saveCause(c, cause, stateNode) {
  try {
    await api(`/api/complaints/${c.id}`, { method: "PUT", body: { name: c.name, started_on: c.started_on, cause } });
    c.cause = cause;
    stateNode.textContent = "gespeichert";
  } catch (error) {
    stateNode.textContent = `nicht gespeichert: ${error.message}`;
  }
}

function openComplaint(c) {
  complaintEditing = c ? c.id : null;
  $("complaint-title").textContent = c ? "Beschwerde bearbeiten" : "Neue Beschwerde";
  $("complaint-name").value = c ? c.name : "";
  $("complaint-start").value = c ? c.started_on : state.today;
  $("complaint-start").max = state.today;
  $("complaint-cause").value = c ? c.cause : "";
  $("complaint-text").value = "";
  document.querySelector(".complaint-first").hidden = Boolean(c);
  $("complaint-delete").hidden = !c;
  $("complaint-error").hidden = true;
  $("complaint-dialog").showModal();
}

async function saveComplaint(event) {
  event.preventDefault();
  const body = { name: $("complaint-name").value, started_on: $("complaint-start").value, cause: $("complaint-cause").value };
  try {
    if (complaintEditing) await api(`/api/complaints/${complaintEditing}`, { method: "PUT", body });
    else await api("/api/complaints", { method: "POST", body: { ...body, text: $("complaint-text").value } });
    $("complaint-dialog").close();
    await loadComplaints();
  } catch (error) {
    $("complaint-error").textContent = error.message;
    $("complaint-error").hidden = false;
  }
}

async function deleteComplaint() {
  if (!confirm("Diese Beschwerde und ihren ganzen Verlauf endgültig löschen? Dein Trainer weiß danach nichts mehr davon. Ist sie nur ausgeheilt, nimm lieber „Beschwerde ist weg“.")) return;
  await complaintAction(() => api(`/api/complaints/${complaintEditing}`, { method: "DELETE" }));
  $("complaint-dialog").close();
}

// --- Heute -------------------------------------------------------------------
function recoveryTile(label, entry, format) {
  const box = el("div", { class: "today-stat" }, el("div", { class: "k", textContent: label }));
  if (entry.value === null) {
    box.append(el("div", { class: "v none", textContent: "kein Wert" }));
    return box;
  }
  box.append(el("div", { class: "v", textContent: format(entry.value) }));
  const notes = [];
  if (entry.average !== null) notes.push(`Ø ${format(entry.average)}`);
  if (entry.date !== state.today) notes.push(`vom ${deShort(entry.date)}`);
  box.append(el("div", { class: "note", textContent: notes.join(" · ") }));
  return box;
}

function recoveryRing(entry) {
  const box = el("div", { class: "recovery-ring" });
  const svg = svgEl("svg", { viewBox: "0 0 64 64", role: "img" });
  svgEl("circle", { cx: 32, cy: 32, r: 27, class: "ring-track" }, svg);
  if (entry.value === null) {
    svg.setAttribute("aria-label", "WHOOP Recovery: noch kein Wert");
    svgEl("text", { x: 32, y: 36, "text-anchor": "middle", class: "ring-none" }, svg).textContent = "–";
  } else {
    const [zone, word] = recoveryZone(entry.value);
    const length = 2 * Math.PI * 27;
    const arc = svgEl("circle", { cx: 32, cy: 32, r: 27, class: `ring-value recovery-${zone}`, transform: "rotate(-90 32 32)",
      "stroke-dasharray": `${(Math.max(2, Math.min(100, entry.value)) / 100 * length).toFixed(1)} ${length.toFixed(1)}` }, svg);
    arc.style.stroke = "currentColor";
    svgEl("text", { x: 32, y: 37, "text-anchor": "middle", class: "ring-text" }, svg).textContent = `${num(entry.value)}%`;
    svg.setAttribute("aria-label", `WHOOP Recovery ${num(entry.value)} Prozent, ${word}`);
    box.title = `${word}${entry.average !== null ? `, Schnitt der 4 Wochen davor: ${num(entry.average)} %` : ""}${entry.date !== state.today ? `, Wert vom ${deShort(entry.date)}` : ""}`;
  }
  box.append(svg, el("span", { class: "k", textContent: "Recovery" }));
  return box;
}

async function loadToday() {
  const t = await api("/api/today");
  $("today-title").textContent = `Heute, ${dayName(t.date)} ${deShort(t.date)}`;
  const rows = [];
  for (const plan of t.planned) if (plan.status !== "done") rows.push(chipFor(plan, "plan"));
  for (const activity of t.activities) rows.push(chipFor(activity, "activity"));
  if (!rows.length) rows.push(el("p", { class: "today-empty", textContent: "Für heute ist kein Training eingetragen." }));
  if (t.next) {
    const facts = shortFacts([t.next.title || SPORT_NAMES[t.next.sport], t.next.duration_min ? hours(t.next.duration_min * 60) : null]);
    rows.push(el("p", { class: "today-next" }, el("span", { class: "muted-inline", textContent: "Als Nächstes: " }), `${dayName(t.next.date)} ${deShort(t.next.date)}, ${facts}`));
  }
  $("today-entries").replaceChildren(...rows);
  const r = t.recovery;
  const tiles = [
    recoveryTile("Schlaf", r.sleep_s, (v) => hours(v)),
    recoveryTile("HRV", r.hrv, (v) => `${num(v)} ms`),
    recoveryTile("Ruhepuls", r.resting_hr, (v) => `${num(v)} bpm`)];
  const ring = r.source === "whoop" ? recoveryRing(r.recovery) : null;
  $("today-recovery").classList.toggle("with-ring", Boolean(ring));
  $("today-recovery").replaceChildren(...(ring ? [ring] : []), ...tiles);
}

async function askToday() {
  setTrainerOpen(true);
  await loadChat({ scroll: true });
  if (chatBusy()) return;
  await sendChat("Was soll ich heute trainieren? Berücksichtige meinen Plan und meine Erholung.");
}

function setTrainerOpen(open) {
  $("trainer").hidden = !open;
  $("trainer-open").hidden = open;
  document.body.classList.toggle("trainer-open", open);
  renderTrends();
  if (open) {
    loadChat({ scroll: true });
    if (innerWidth > 640) $("chat-input").focus();
  }
}

// --- Tag: Einträge und Formular ---------------------------------------------
function resetPlanForm(date, plan) {
  state.editing = plan ? plan.id : null;
  $("plan-form-title").textContent = plan ? "Training bearbeiten" : "Training eintragen";
  $("plan-sport").value = plan ? plan.sport : "run";
  $("plan-date").value = plan ? plan.date : date;
  $("plan-title").value = plan ? plan.title : "";
  $("plan-duration").value = plan && plan.duration_min !== null ? plan.duration_min : "";
  $("plan-distance").value = plan && plan.distance_km !== null ? plan.distance_km : "";
  $("plan-intensity").value = plan ? plan.intensity : "";
  $("plan-notes").value = plan ? plan.notes : "";
  $("plan-delete").hidden = !plan;
  $("plan-cancel").hidden = !plan;
  $("plan-error").hidden = true;
}

function renderDayEntries(date) {
  const { planned, activities } = state.calendar;
  const rows = [];
  for (const plan of planned.filter((p) => p.date === date)) {
    const facts = [plan.duration_min ? `${plan.duration_min} Min.` : null, plan.distance_km ? `${num(plan.distance_km, 1)} km` : null, plan.intensity || null].filter(Boolean).join(" · ");
    const body = el("span", {}, el("b", { textContent: plan.title || SPORT_NAMES[plan.sport] }), el("br"), el("small", { textContent: [SPORT_NAMES[plan.sport], facts].filter(Boolean).join(" · ") }));
    if (plan.google_uid) {
      rows.push(el("div", { class: `entry google sport-${plan.sport}` }, body,
        el("div", { class: "google-side" },
          el("span", { class: "state", textContent: STATUS_TEXT[plan.status] }),
          el("span", { class: "muted-inline small", textContent: plan.notes }),
          el("button", { type: "button", textContent: plan.date <= state.today ? "Nicht gemacht" : "Ausblenden", on: { click: () => hideGooglePlan(plan.id) } }))));
      continue;
    }
    rows.push(el("button", { class: `entry sport-${plan.sport}`, type: "button", on: { click: () => resetPlanForm(date, plan) } },
      body, el("span", { class: "state", textContent: STATUS_TEXT[plan.status] })));
  }
  for (const activity of activities.filter((a) => a.date === date)) {
    rows.push(el("button", { class: `entry sport-${activity.sport}`, type: "button", on: { click: () => openActivity(activity.id) } },
      el("span", {}, el("b", { textContent: activityLabel(activity) }), el("br"), el("small", { textContent: [hours(activity.duration_s), distance(activity.sport, activity.distance_m)].filter(Boolean).join(" · ") })),
      el("span", { class: "state", textContent: "absolviert ›" })));
  }
  $("day-entries").replaceChildren(...rows);
}

function openDay(date, planId) {
  state.dayDate = date;
  $("day-title").textContent = `${dayName(date)}, ${de(date)}`;
  renderDayEntries(date);
  const chosen = planId ? state.calendar.planned.find((p) => p.id === planId) : null;
  resetPlanForm(date, chosen && !chosen.google_uid ? chosen : null);
  if (!$("day-dialog").open) $("day-dialog").showModal();
}

async function hideGooglePlan(id) {
  try {
    await api(`/api/planned/${id}`, { method: "DELETE" });
    await Promise.all([refreshPlanViews(), loadRecent(), loadTrends()]);
  } catch (error) {
    showBanner(error.message);
  }
}

async function savePlan(event) {
  event.preventDefault();
  const value = (id) => $(id).value.trim();
  const body = {
    date: value("plan-date"), sport: value("plan-sport"), title: value("plan-title"),
    duration_min: value("plan-duration") === "" ? null : Number(value("plan-duration")),
    distance_km: value("plan-distance") === "" ? null : Number(value("plan-distance")),
    intensity: value("plan-intensity"), notes: value("plan-notes"),
  };
  try {
    if (state.editing) await api(`/api/planned/${state.editing}`, { method: "PUT", body });
    else await api("/api/planned", { method: "POST", body });
    state.dayDate = body.date;
    await refreshPlanViews();
    $("day-title").textContent = `${dayName(body.date)}, ${de(body.date)}`;
    resetPlanForm(body.date);
  } catch (error) {
    $("plan-error").textContent = error.message;
    $("plan-error").hidden = false;
  }
}

async function deletePlan() {
  if (!state.editing) return;
  if (!confirm("Diesen Eintrag löschen?")) return;
  try {
    await api(`/api/planned/${state.editing}`, { method: "DELETE" });
    await refreshPlanViews();
    resetPlanForm(state.dayDate);
  } catch (error) {
    $("plan-error").textContent = error.message;
    $("plan-error").hidden = false;
  }
}

async function refreshPlanViews() {
  await Promise.all([loadCalendar(), loadOverview(), loadToday(), loadStrip()]);
}

// --- Woche im Blick -------------------------------------------------------
async function loadStrip() {
  const start = parse(state.stripWeek);
  const end = addDays(start, 6);
  const data = await api(`/api/calendar?start=${iso(start)}&end=${iso(end)}`);
  const current = state.stripWeek === state.currentWeek;
  $("strip-title").textContent = current ? "Woche im Blick" : `Woche ${deShort(iso(start))} bis ${deShort(iso(end))}`;
  $("strip-today").disabled = current;
  const days = [];
  for (let i = 0; i < 7; i++) {
    const key = iso(addDays(start, i));
    const classes = ["strip-day"];
    if (key === state.today) classes.push("today");
    if (key < state.today) classes.push("past");
    const cell = el("div", { class: classes.join(" "), on: { click: () => openDay(key) } },
      el("div", { class: "strip-head" }, el("span", { textContent: DOW[i] }), el("span", { class: "d", textContent: deShort(key) })));
    makeDropTarget(cell, key);
    const items = [];
    for (const plan of data.planned.filter((p) => p.date === key && p.status !== "done")) {
      const chip = chipFor(plan, "plan");
      if (!plan.google_uid) makeDraggable(chip, plan);
      items.push(chip);
    }
    for (const activity of data.activities.filter((a) => a.date === key)) items.push(chipFor(activity, "activity"));
    cell.append(...(items.length ? items : [el("span", { class: "strip-empty", textContent: key < state.today ? "frei" : "" })]));
    days.push(cell);
  }
  $("week-strip").replaceChildren(...days);
}

function shiftStrip(weeks) {
  state.stripWeek = weeks === 0 ? state.currentWeek : iso(addDays(parse(state.stripWeek), 7 * weeks));
  loadStrip().catch((e) => showBanner(e.message));
}

// --- Letzte Trainings ------------------------------------------------------
const ANALYSIS_STATE = { done: "ausgewertet", pending: "wird ausgewertet", running: "wird ausgewertet", failed: "fehlgeschlagen" };

const RECENT_PAGE = 20;
const recentState = { rows: [] };

async function loadRecent() {
  const count = Math.max(RECENT_PAGE, recentState.rows.length);
  const rows = await api(`/api/activities?limit=${count}`);
  recentState.rows = rows;
  renderRecent(rows.length === count);
  return rows;
}

async function loadMoreRecent() {
  const more = await api(`/api/activities?limit=${RECENT_PAGE}&offset=${recentState.rows.length}`);
  recentState.rows = recentState.rows.concat(more);
  renderRecent(more.length === RECENT_PAGE);
}

function renderRecent(hasMore) {
  const rows = recentState.rows;
  $("recent-more").hidden = !hasMore;
  if (!rows.length) {
    const empty = () => el("p", { class: "empty", textContent: "Noch keine Trainings. Sie erscheinen hier nach dem ersten Garmin-Abgleich." });
    $("recent").replaceChildren(empty());
    $("recent-short").replaceChildren(empty());
    return;
  }
  $("recent-short").replaceChildren(...rows.slice(0, 4).map(recentRow));
  $("recent").replaceChildren(...rows.map(recentRow));
}

function recentRow(a) {
    const speed = pace(a.sport, a.avg_speed);
    const facts = [`${dayName(a.date)} ${deShort(a.date)}`, hours(a.duration_s), distance(a.sport, a.distance_m), speed && speed.v, a.avg_hr ? `${num(a.avg_hr)} bpm` : null].filter(Boolean).join(" · ");
    return el("button", { class: "recent-row", type: "button", on: { click: () => (a.calendar_only ? openDay(a.date) : openActivity(a.id)) } },
      el("i", { class: `swatch sport-${a.sport}`, title: SPORT_NAMES[a.sport] }),
      el("span", { class: "name", textContent: activityLabel(a) }),
      el("span", { class: "state", textContent: a.calendar_only ? "laut Kalender" : ANALYSIS_STATE[a.analysis_status] || "" }),
      el("span", { class: "meta", textContent: facts }));
}

// --- Auswertung darstellen ------------------------------------------------
// Baut nur Textknoten und feste Elemente, der Text kann also kein HTML einschleusen.
function inline(parent, text) {
  text.split(/(\*\*[^*]+\*\*)/).forEach((part) => {
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) parent.append(el("strong", { textContent: part.slice(2, -2) }));
    else if (part) parent.append(part);
  });
  return parent;
}

function renderMarkdown(text) {
  const nodes = [];
  let list = null;
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) { list = null; continue; }
    const bullet = line.match(/^[-*]\s+(.*)$/);
    if (bullet) {
      if (!list) { list = el("ul"); nodes.push(list); }
      list.append(inline(el("li"), bullet[1]));
      continue;
    }
    list = null;
    const heading = line.match(/^#{1,6}\s+(.*)$/);
    nodes.push(heading ? el("p", {}, el("strong", { textContent: heading[1].replaceAll("*", "") })) : inline(el("p"), line));
  }
  return nodes;
}

function renderAnalysis(target, analysis, emptyText) {
  if (!analysis) {
    target.replaceChildren(el("p", { class: "empty", textContent: emptyText }));
    return false;
  }
  if (analysis.status === "pending" || analysis.status === "running") {
    target.replaceChildren(el("div", { class: "status" }, el("span", { class: "spin" }), "Claude wertet aus, das dauert meist unter einer Minute."));
    return true;
  }
  if (analysis.status === "failed") {
    target.replaceChildren(el("p", { class: "error", textContent: `Auswertung fehlgeschlagen: ${analysis.error || "unbekannter Fehler"}` }));
    return false;
  }
  const when = new Date(analysis.finished_at * 1000).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" });
  target.replaceChildren(...renderMarkdown(analysis.result), el("div", { class: "when", textContent: `Ausgewertet am ${when}` }));
  return false;
}

function pollAnalysis(name, scope, ref, target, emptyText, onDone) {
  clearTimeout(timers[name]);
  const tick = async () => {
    let analysis;
    try {
      analysis = await api(`/api/analyses/latest?scope=${scope}&ref=${encodeURIComponent(ref)}`);
    } catch { return; }
    const waiting = renderAnalysis(target, analysis, emptyText);
    if (waiting) timers[name] = setTimeout(tick, 3000);
    else if (onDone) onDone(analysis);
  };
  return tick();
}

// --- Aktivität ------------------------------------------------------------
async function openActivity(id) {
  state.activityId = id;
  let a;
  try {
    a = await api(`/api/activities/${id}`);
  } catch (error) {
    showBanner(error.message);
    return;
  }
  $("activity-title").textContent = activityLabel(a);
  $("activity-sub").textContent = `${SPORT_NAMES[a.sport]} · ${dayName(a.date)}, ${de(a.date)}, ${a.start_local.slice(11, 16)} Uhr${a.id < 0 ? " · manuell eingetragen" : ""}`;
  const speed = pace(a.sport, a.avg_speed);
  const metrics = [
    ["Dauer", a.duration_s ? clock(a.duration_s) : null],
    ["Distanz", distance(a.sport, a.distance_m)],
    speed && [speed.k, speed.v],
    ["Ø Puls", a.avg_hr ? `${num(a.avg_hr)} bpm` : null],
    ["Max. Puls", a.max_hr ? `${num(a.max_hr)} bpm` : null],
    ["Ø Leistung", a.avg_power ? `${num(a.avg_power)} W` : null],
    ["NP", a.norm_power ? `${num(a.norm_power)} W` : null],
    ["TSS", a.tss ? num(a.tss) : null],
    ["Trittfrequenz", a.avg_cadence ? num(a.avg_cadence) : null],
    ["Höhenmeter", a.elevation_m ? `${num(a.elevation_m)} m` : null],
    ["Aerober Effekt", a.te_aerobic ? num(a.te_aerobic, 1) : null],
    ["Anaerober Effekt", a.te_anaerobic ? num(a.te_anaerobic, 1) : null],
    ["Kalorien", a.calories ? num(a.calories) : null],
  ].filter((m) => m && m[1]);
  $("activity-metrics").replaceChildren(...metrics.map(([k, v]) =>
    el("div", { class: "metric" }, el("div", { class: "k", textContent: k }), el("div", { class: "v", textContent: v }))));
  $("activity-plan").replaceChildren(...a.plan.map((p) => {
    const facts = [p.duration_min ? `${p.duration_min} Min.` : null, p.distance_km ? `${num(p.distance_km, 1)} km` : null, p.intensity || null].filter(Boolean).join(" · ");
    return el("div", { class: "plan-box" }, el("b", { textContent: "Geplant: " }), [p.title || SPORT_NAMES[p.sport], facts].filter(Boolean).join(" · "));
  }));
  renderLaps(a);
  state.activity = a;
  state.savedNote = a.note || "";
  $("activity-note").value = state.savedNote;
  $("note-state").textContent = "Wird automatisch gespeichert und bei der nächsten Auswertung berücksichtigt.";
  $("activity-manual").hidden = a.id >= 0;
  if (!$("activity-dialog").open) $("activity-dialog").showModal();
  $("activity-analyze").textContent = a.analysis ? "Neu auswerten" : "Auswerten";
  await pollAnalysis("activity", "activity", String(id), $("activity-analysis"), "Für diese Einheit gibt es noch keine Auswertung.", () => {
    $("activity-analyze").disabled = false;
    loadRecent();
  });
}

function renderLaps(a) {
  if (!a.laps.length) { $("activity-laps").replaceChildren(); return; }
  const head = el("tr", {}, ...["Abschnitt", "Dauer", "Distanz", "Tempo", "Puls", "Leistung"].map((t) => el("th", { textContent: t })));
  const rows = a.laps.map((lap, i) => {
    const speed = pace(a.sport, lap.avg_speed);
    const cells = [lap.label || i + 1, lap.duration_s ? clock(lap.duration_s) : "–", distance(a.sport, lap.distance_m) || "–", speed ? speed.v : "–", lap.avg_hr ? num(lap.avg_hr) : "–", lap.avg_power ? `${num(lap.avg_power)} W` : "–"];
    return el("tr", {}, ...cells.map((c) => el("td", { textContent: c })));
  });
  $("activity-laps").replaceChildren(el("h3", { textContent: "Abschnitte" }), el("div", { class: "table-wrap" }, el("table", {}, el("thead", {}, head), el("tbody", {}, ...rows))));
}

async function saveNote() {
  const note = $("activity-note").value.trim();
  if (state.activityId === null || note === state.savedNote) return;
  await api(`/api/activities/${state.activityId}/note`, { method: "PUT", body: { note } });
  state.savedNote = note;
  $("note-state").textContent = "Notiz gespeichert.";
}

function saveNoteQuietly() {
  saveNote().catch((error) => { $("note-state").textContent = `Notiz nicht gespeichert: ${error.message}`; });
}

function parseDuration(text) {
  const value = text.trim();
  const clockForm = value.match(/^(\d{1,2}):([0-5]\d)$/);
  if (clockForm) return Number(clockForm[1]) * 60 + Number(clockForm[2]);
  return /^\d+([.,]\d+)?$/.test(value) ? Number(value.replace(",", ".")) : null;
}

function openManual(activity, date) {
  state.manualId = activity ? activity.id : null;
  const now = new Date();
  $("manual-title").textContent = activity ? "Training bearbeiten" : "Training hinzufügen";
  $("manual-hint").hidden = Boolean(activity);
  $("manual-sport").value = activity ? activity.sport : "run";
  $("manual-name").value = activity ? activity.name || "" : "";
  $("manual-date").value = activity ? activity.date : date || iso(now);
  $("manual-date").max = iso(now);
  $("manual-time").value = activity ? activity.start_local.slice(11, 16) : `${pad(now.getHours())}:${pad(now.getMinutes())}`;
  const minutes = activity ? Math.round(activity.duration_s / 60) : null;
  $("manual-duration").value = minutes ? `${Math.floor(minutes / 60)}:${pad(minutes % 60)}` : "";
  $("manual-distance").value = activity && activity.distance_m ? Number((activity.distance_m / 1000).toFixed(2)) : "";
  $("manual-hr").value = activity && activity.avg_hr ? activity.avg_hr : "";
  $("manual-maxhr").value = activity && activity.max_hr ? activity.max_hr : "";
  $("manual-power").value = activity && activity.avg_power ? activity.avg_power : "";
  $("manual-elevation").value = activity && activity.elevation_m ? activity.elevation_m : "";
  $("manual-note").value = activity ? activity.note || "" : "";
  $("manual-error").hidden = true;
  $("manual-dialog").showModal();
}

async function saveManual(event) {
  event.preventDefault();
  const fail = (message) => { $("manual-error").textContent = message; $("manual-error").hidden = false; };
  const duration = parseDuration($("manual-duration").value);
  if (!duration) return fail("Bitte die Dauer in Minuten (90) oder als Stunden:Minuten (1:30) eingeben.");
  const optional = (id) => ($(id).value === "" ? null : Number($(id).value));
  const body = {
    sport: $("manual-sport").value, name: $("manual-name").value, date: $("manual-date").value, time: $("manual-time").value,
    duration_min: duration, distance_km: optional("manual-distance"), avg_hr: optional("manual-hr"), max_hr: optional("manual-maxhr"),
    avg_power: optional("manual-power"), elevation_m: optional("manual-elevation"), note: $("manual-note").value,
  };
  try {
    const editing = state.manualId;
    const saved = editing
      ? await api(`/api/activities/${editing}`, { method: "PUT", body })
      : await api("/api/activities", { method: "POST", body });
    $("manual-dialog").close();
    $("day-dialog").close();
    await refreshAll();
    await openActivity(editing || saved.id);
  } catch (error) {
    fail(error.message);
  }
}

async function deleteManual() {
  if (!confirm("Dieses manuell eingetragene Training und seine Auswertung löschen?")) return;
  try {
    await api(`/api/activities/${state.activityId}`, { method: "DELETE" });
    state.savedNote = $("activity-note").value.trim();
    state.activityId = null;
    $("activity-dialog").close();
    await refreshAll();
  } catch (error) {
    showBanner(error.message);
  }
}

async function analyzeActivity() {
  const button = $("activity-analyze");
  button.disabled = true;
  try {
    await saveNote();
    await api("/api/analyses", { method: "POST", body: { scope: "activity", ref: String(state.activityId) } });
    await pollAnalysis("activity", "activity", String(state.activityId), $("activity-analysis"), "", () => {
      button.disabled = false;
      button.textContent = "Neu auswerten";
      loadRecent();
    });
  } catch (error) {
    button.disabled = false;
    showBanner(error.message);
  }
}

// --- Wochenauswertung ---------------------------------------------------
function loadWeek() {
  const start = state.weekStart;
  const end = iso(addDays(parse(start), 6));
  const label = start === state.currentWeek ? "diese Woche" : `${deShort(start)} bis ${deShort(end)}`;
  $("week-title").textContent = `Wochenauswertung: ${label}`;
  $("next-week").disabled = start >= state.currentWeek;
  return pollAnalysis("week", "week", start, $("week-analysis"), "Für diese Woche gibt es noch keine Auswertung.");
}

async function analyzeWeek() {
  try {
    await api("/api/analyses", { method: "POST", body: { scope: "week", ref: state.weekStart } });
    await loadWeek();
  } catch (error) {
    showBanner(error.message);
  }
}

// --- Diagramme --------------------------------------------------------------
const SVG = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs = {}, parent) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (parent) parent.append(node);
  return node;
}

// Obergrenze für vier Gitterlinien, sodass jede Linie auf einem runden Wert liegt.
function niceMax(value) {
  if (value <= 0) return 4;
  const tick = value / 4;
  const pow = 10 ** Math.floor(Math.log10(tick));
  for (const step of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (step * pow >= tick) return step * pow * 4;
  return 40 * pow;
}

function showTooltip(event, build) {
  const tip = $("tooltip");
  tip.replaceChildren(...build());
  tip.hidden = false;
  const box = tip.getBoundingClientRect();
  const x = Math.min(event.clientX + 14, innerWidth - box.width - 8);
  const y = Math.max(8, event.clientY - box.height - 10);
  tip.style.left = `${x}px`;
  tip.style.top = `${y}px`;
}
const hideTooltip = () => { $("tooltip").hidden = true; };

function emptyChart(container, text) {
  container.replaceChildren(el("p", { class: "empty", textContent: text }));
}

function roundedTop(x, y, w, h, r) {
  const radius = Math.min(r, h, w / 2);
  return `M${x},${y + h}V${y + radius}Q${x},${y} ${x + radius},${y}H${x + w - radius}Q${x + w},${y} ${x + w},${y + radius}V${y + h}Z`;
}

function volumeChart(weeks, container, legend, table) {
  legend.replaceChildren(...STACK.map((s) => el("span", {}, el("i", { class: `swatch sport-${s}` }), SPORT_NAMES[s])));
  const total = (w) => STACK.reduce((sum, s) => sum + w[s], 0);
  if (!weeks.some((w) => total(w) > 0)) {
    emptyChart(container, "Sobald Trainings da sind, siehst du hier den Umfang pro Woche.");
    table.replaceChildren();
    return;
  }
  const W = Math.max(container.clientWidth, 280);
  const H = container.clientHeight;
  const m = { l: 34, r: 6, t: 8, b: 22 };
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Trainingsstunden pro Woche nach Sportart" });
  const max = niceMax(Math.max(...weeks.map(total)));
  const y = (v) => H - m.b - (v / max) * (H - m.t - m.b);
  for (let i = 0; i <= 4; i++) {
    const v = (max / 4) * i;
    svgEl("line", { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), class: i ? "grid-line" : "base-line" }, svg);
    svgEl("text", { x: m.l - 6, y: y(v) + 4, "text-anchor": "end" }, svg).textContent = `${num(v, v % 1 ? 1 : 0)} h`;
  }
  const slot = (W - m.l - m.r) / weeks.length;
  const barWidth = Math.max(2, Math.min(24, slot - (slot > 10 ? 4 : 1)));
  const labelEvery = Math.max(1, Math.ceil(48 / slot));
  weeks.forEach((week, i) => {
    const x = m.l + slot * i + (slot - barWidth) / 2;
    let stacked = 0;
    const present = STACK.filter((s) => week[s] > 0);
    present.forEach((sport, index) => {
      const top = y(stacked + week[sport]);
      const bottom = y(stacked);
      const height = Math.max(1, bottom - top);
      const gap = index > 0 ? 2 : 0;
      const shape = index === present.length - 1
        ? svgEl("path", { d: roundedTop(x, top, barWidth, Math.max(1, height - gap), 4) }, svg)
        : svgEl("rect", { x, y: top, width: barWidth, height: Math.max(1, height - gap) }, svg);
      shape.style.fill = `var(--${sport})`;
      stacked += week[sport];
    });
    if (i % labelEvery === (weeks.length - 1) % labelEvery) {
      svgEl("text", { x: x + barWidth / 2, y: H - 6, "text-anchor": "middle" }, svg).textContent = deShort(week.week);
    }
    const hit = svgEl("rect", { x: m.l + slot * i, y: m.t, width: slot, height: H - m.t - m.b, class: "hit" }, svg);
    const showWeek = (event) => showTooltip(event, () => [
      el("b", { textContent: `Woche ab ${deShort(week.week)}: ${hours(total(week) * 3600)}` }),
      ...present.map((s) => el("div", {}, el("span", { class: "s" }, el("i", { class: `swatch sport-${s}` }), SPORT_NAMES[s]), el("span", { textContent: hours(week[s] * 3600) }))),
    ]);
    hit.addEventListener("pointermove", showWeek);
    hit.addEventListener("pointerdown", showWeek);
    hit.addEventListener("pointerleave", (event) => { if (event.pointerType !== "touch") hideTooltip(); });
  });
  container.replaceChildren(svg);
  const head = el("tr", {}, el("th", { textContent: "Woche ab" }), ...STACK.map((s) => el("th", { textContent: SPORT_NAMES[s] })), el("th", { textContent: "Gesamt" }));
  const rows = [...weeks].reverse().map((w) => el("tr", {}, el("td", { textContent: de(w.week) }), ...STACK.map((s) => el("td", { textContent: hours(w[s] * 3600) })), el("td", { textContent: hours(total(w) * 3600) })));
  table.replaceChildren(el("div", { class: "table-wrap" }, el("table", {}, el("thead", {}, head), el("tbody", {}, ...rows))));
}

function lineChart(container, points, format) {
  if (points.length < 2) {
    emptyChart(container, "Noch zu wenig Daten.");
    return;
  }
  const W = Math.max(container.clientWidth, 220);
  const H = container.clientHeight;
  const m = { l: 32, r: 8, t: 8, b: 20 };
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" });
  const values = points.map((p) => p.v);
  let low = Math.min(...values);
  let high = Math.max(...values);
  const pad_ = Math.max((high - low) * 0.15, 1);
  low = Math.floor(low - pad_);
  high = Math.ceil(high + pad_);
  const t0 = parse(points[0].date).getTime();
  const t1 = parse(points[points.length - 1].date).getTime();
  const x = (p) => m.l + ((parse(p.date).getTime() - t0) / (t1 - t0)) * (W - m.l - m.r);
  const y = (v) => H - m.b - ((v - low) / (high - low)) * (H - m.t - m.b);
  [low, (low + high) / 2, high].forEach((v, i) => {
    svgEl("line", { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), class: i ? "grid-line" : "base-line" }, svg);
    svgEl("text", { x: m.l - 5, y: y(v) + 4, "text-anchor": "end" }, svg).textContent = num(v, v % 1 ? 1 : 0);
  });
  svgEl("text", { x: m.l, y: H - 5 }, svg).textContent = deShort(points[0].date);
  svgEl("text", { x: W - m.r, y: H - 5, "text-anchor": "end" }, svg).textContent = deShort(points[points.length - 1].date);
  // Lücken (Tage ohne Wert) werden nicht überbrückt, sobald sie länger als zwei Tage sind.
  let path = "";
  points.forEach((p, i) => {
    const gap = i && (parse(p.date) - parse(points[i - 1].date)) / 86400000 > 2;
    path += `${i === 0 || gap ? "M" : "L"}${x(p).toFixed(1)},${y(p.v).toFixed(1)}`;
  });
  svgEl("path", { d: path, class: "series-line" }, svg);
  const cross = svgEl("line", { y1: m.t, y2: H - m.b, class: "cross", visibility: "hidden" }, svg);
  const dot = svgEl("circle", { r: 4, class: "dot", visibility: "hidden" }, svg);
  const hit = svgEl("rect", { x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, class: "hit" }, svg);
  const showPoint = (event) => {
    const box = svg.getBoundingClientRect();
    const px = ((event.clientX - box.left) / box.width) * W;
    const nearest = points.reduce((best, p) => (Math.abs(x(p) - px) < Math.abs(x(best) - px) ? p : best));
    cross.setAttribute("x1", x(nearest)); cross.setAttribute("x2", x(nearest)); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", x(nearest)); dot.setAttribute("cy", y(nearest.v)); dot.setAttribute("visibility", "visible");
    showTooltip(event, () => [el("b", { textContent: `${dayName(nearest.date)}, ${de(nearest.date)}` }), el("div", {}, el("span", { textContent: format(nearest.v) }))]);
  };
  hit.addEventListener("pointermove", showPoint);
  hit.addEventListener("pointerdown", showPoint);
  hit.addEventListener("pointerleave", (event) => {
    if (event.pointerType === "touch") return;
    cross.setAttribute("visibility", "hidden");
    dot.setAttribute("visibility", "hidden");
    hideTooltip();
  });
  container.replaceChildren(svg);
}

function weekTotals(week, sport) {
  const sports = sport === "all" ? STACK : [sport];
  const sum = (field) => sports.reduce((total, s) => total + (week[field][s] || 0), 0);
  return { time: sports.reduce((total, s) => total + week[s], 0), km: sum("km"), hm: sum("hm"), count: sum("count") };
}

function metricText(metric, value) {
  if (metric === "time") return hours(value * 3600);
  if (metric === "km") return `${num(value, value < 100 ? 1 : 0)} km`;
  return `${num(value)} hm`;
}

function weeklyHero(weeks, week) {
  const totals = weekTotals(week, weekly.sport);
  const current = week.week === state.currentWeek;
  $("weekly-label").textContent = current ? "Diese Woche, bisher" : `Woche ab ${de(week.week)}`;
  $("weekly-value").textContent = metricText(weekly.metric, totals[weekly.metric]);
  // Die laufende Woche ist noch nicht vorbei und zählt nicht für den Durchschnitt.
  const finished = weeks.filter((w) => w.week !== state.currentWeek);
  const average = finished.length ? finished.reduce((sum, w) => sum + weekTotals(w, weekly.sport)[weekly.metric], 0) / finished.length : 0;
  const unit = totals.count === 1 ? "Einheit" : "Einheiten";
  const facts = [`${totals.count} ${unit}`];
  for (const metric of ["time", "km", "hm"]) {
    if (metric !== weekly.metric && totals[metric] > 0) facts.push(metricText(metric, totals[metric]));
  }
  facts.push(`Ø ${metricText(weekly.metric, average)} pro Woche im Zeitraum`);
  $("weekly-note").textContent = facts.join(" · ");
}

function weeklyChart() {
  const all = trendData.weeks;
  const weeks = all.slice(-weekly.range);
  const container = $("chart-weekly");
  const values = weeks.map((w) => weekTotals(w, weekly.sport)[weekly.metric]);
  const selected = weeks.find((w) => w.week === weekly.pinned) || weeks[weeks.length - 1];
  weeklyHero(weeks, selected);
  const color = weekly.sport === "all" ? "var(--text)" : `var(--${weekly.sport})`;
  const stacked = weekly.sport === "all" && weekly.metric === "time";
  $("weekly-legend").hidden = !stacked;
  if (stacked) {
    volumeChart(weeks, container, $("weekly-legend"), $("weekly-table"));
    return;
  }
  if (!values.some((v) => v > 0)) {
    emptyChart(container, "Für diese Auswahl gibt es im Zeitraum keine Werte.");
    $("weekly-table").replaceChildren();
    return;
  }
  const W = Math.max(container.clientWidth, 280);
  const H = container.clientHeight;
  const m = { l: 46, r: 12, t: 12, b: 22 };
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `Wochenumfang, ${WEEKLY_SPORTS.find((s) => s[0] === weekly.sport)[1]}` });
  const max = niceMax(Math.max(...values));
  const x = (i) => m.l + (i / Math.max(1, weeks.length - 1)) * (W - m.l - m.r);
  const y = (v) => H - m.b - (v / max) * (H - m.t - m.b);
  for (let i = 0; i <= 4; i++) {
    const v = (max / 4) * i;
    svgEl("line", { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), class: i ? "grid-line" : "base-line" }, svg);
    svgEl("text", { x: m.l - 6, y: y(v) + 4, "text-anchor": "end" }, svg).textContent = weekly.metric === "time" ? `${num(v, v % 1 ? 1 : 0)} h` : num(v, v % 1 ? 1 : 0);
  }
  const every = Math.ceil(weeks.length / Math.max(2, Math.floor((W - m.l - m.r) / 54)));
  weeks.forEach((week, i) => {
    if ((weeks.length - 1 - i) % every === 0) svgEl("text", { x: x(i), y: H - 6, "text-anchor": "middle" }, svg).textContent = deShort(week.week);
  });
  const line = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const area = svgEl("path", { d: `${line}L${x(values.length - 1).toFixed(1)},${y(0)}L${x(0).toFixed(1)},${y(0)}Z`, class: "weekly-area" }, svg);
  area.style.fill = color;
  const path = svgEl("path", { d: line, class: "weekly-line" }, svg);
  path.style.stroke = color;
  if (weeks.length <= 26) {
    values.forEach((v, i) => {
      const dot = svgEl("circle", { cx: x(i), cy: y(v), r: 4, class: "weekly-dot" }, svg);
      dot.style.fill = color;
    });
  }
  const cross = svgEl("line", { y1: m.t, y2: H - m.b, class: "cross" }, svg);
  const focus = svgEl("circle", { r: 6, class: "weekly-dot" }, svg);
  focus.style.fill = color;
  const mark = (i) => {
    cross.setAttribute("x1", x(i));
    cross.setAttribute("x2", x(i));
    focus.setAttribute("cx", x(i));
    focus.setAttribute("cy", y(values[i]));
    weeklyHero(weeks, weeks[i]);
  };
  mark(weeks.indexOf(selected));
  const nearest = (event) => {
    const box = svg.getBoundingClientRect();
    const px = ((event.clientX - box.left) / box.width) * W;
    const step = (W - m.l - m.r) / Math.max(1, weeks.length - 1);
    return Math.max(0, Math.min(weeks.length - 1, Math.round((px - m.l) / step)));
  };
  const hit = svgEl("rect", { x: 0, y: 0, width: W, height: H, class: "hit" }, svg);
  const show = (event) => {
    const i = nearest(event);
    mark(i);
    showTooltip(event, () => [el("b", { textContent: `Woche ab ${deShort(weeks[i].week)}` }), el("div", {}, el("span", { textContent: metricText(weekly.metric, values[i]) }))]);
  };
  hit.addEventListener("pointermove", show);
  hit.addEventListener("pointerdown", (event) => { show(event); weekly.pinned = weeks[nearest(event)].week; });
  hit.addEventListener("pointerleave", (event) => {
    if (event.pointerType === "touch") return;
    hideTooltip();
    mark(weeks.indexOf(weeks.find((w) => w.week === weekly.pinned) || weeks[weeks.length - 1]));
  });
  container.replaceChildren(svg);
  const head = el("tr", {}, ...["Woche ab", "Einheiten", "Zeit", "Distanz", "Höhenmeter"].map((t) => el("th", { textContent: t })));
  const rows = [...weeks].reverse().map((w) => {
    const t = weekTotals(w, weekly.sport);
    return el("tr", {}, ...[de(w.week), t.count, hours(t.time * 3600), metricText("km", t.km), metricText("hm", t.hm)].map((c) => el("td", { textContent: c })));
  });
  $("weekly-table").replaceChildren(el("div", { class: "table-wrap" }, el("table", {}, el("thead", {}, head), el("tbody", {}, ...rows))));
}

function segmented(id, options, key) {
  const render = () => {
    $(id).replaceChildren(...options.map(([value, label]) => el("button", {
      type: "button", textContent: label, ariaPressed: String(weekly[key] === value),
      on: { click: () => { weekly[key] = value; render(); weeklyChart(); } },
    })));
  };
  render();
}

let trendData = null;
function renderTrends() {
  // Versteckt hätten die Diagramme keine Größe. Gezeichnet wird beim Öffnen des Reiters.
  if (!trendData || $("p-trends").hidden) return;
  weeklyChart();
  const pick = (field, scale = 1) => trendData.wellness.filter((d) => d[field] !== null).map((d) => ({ date: d.date, v: d[field] / scale }));
  // Mit WHOOP-Daten zeigen alle Kurven WHOOP, denn Garmin- und WHOOP-HRV sind nicht vergleichbar.
  const w = trendData.wellness.some((d) => d.whoop_recovery !== null) ? "whoop_" : "";
  $("chart-recovery-box").hidden = !w;
  $("chart-hrv-title").textContent = w ? "HRV (ms, WHOOP)" : "HRV (ms)";
  if (w) lineChart($("chart-recovery"), pick("whoop_recovery"), (v) => `${num(v)} %`);
  lineChart($("chart-hrv"), pick(`${w}hrv`), (v) => `${num(v)} ms`);
  lineChart($("chart-rhr"), pick(`${w}resting_hr`), (v) => `${num(v)} bpm`);
  lineChart($("chart-sleep"), pick(`${w}sleep_s`, 3600), (v) => `${num(v, 1)} Stunden`);
}

// --- Einstellungen ---------------------------------------------------------
function renderCalendarState(s) {
  if (!s.calendar_count) {
    $("calendar-state").textContent = "Noch nicht verbunden. Verbunden berücksichtigt der Trainer deine Termine beim Planen.";
    return;
  }
  const when = s.calendar_last_fetch ? new Date(s.calendar_last_fetch * 1000).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" }) : "noch nie";
  const word = s.calendar_count === 1 ? "1 Kalender" : `${s.calendar_count} Kalender`;
  $("calendar-state").textContent = `${word} verbunden, zuletzt gelesen: ${when}. Trag nur dann etwas ein, wenn du die Adressen ersetzen willst.`;
}

async function saveCalendar(event) {
  event.preventDefault();
  const error = $("calendar-error");
  error.hidden = true;
  if (!$("set-calendar").value.trim()) return;
  const button = event.submitter;
  button.disabled = true;
  try {
    const result = await api("/api/settings/calendar", { method: "POST", body: { urls: $("set-calendar").value } });
    $("set-calendar").value = "";
    renderCalendarState(await api("/api/settings"));
    $("calendar-remove").hidden = false;
    $("set-calendar").required = false;
    $("calendar-state").textContent += ` ${result.trainings} Trainings und ${result.others} andere Termine gefunden.`;
    $("calendar-terms").hidden = false;
    await refreshAll();
    await loadOverview();
  } catch (problem) {
    error.textContent = problem.message;
    error.hidden = false;
  } finally {
    button.disabled = false;
  }
}

async function removeCalendar() {
  if (!confirm("Verbindung zum Google Kalender trennen? Die Adresse und die gespeicherten Termine werden vom Server gelöscht. In deinem Google Kalender ändert sich nichts.")) return;
  await api("/api/settings/calendar", { method: "DELETE" });
  await openSettings();
  await loadOverview();
}

function renderWhoopState(s) {
  $("whoop-redirect").textContent = s.whoop_redirect_uri;
  $("set-whoop-id").value = s.whoop_client_id;
  $("set-whoop-secret").value = "";
  $("set-whoop-secret").placeholder = s.whoop_client_id ? "gespeichert" : "";
  $("whoop-error").hidden = true;
  $("whoop-remove").hidden = !s.whoop_connected;
  $("whoop-help").open = !s.whoop_client_id;
  if (!s.whoop_connected) {
    $("whoop-state").textContent = "Noch nicht verbunden. Verbunden zeigt das Dashboard deine WHOOP-Recovery, und der Trainer plant damit.";
  } else {
    const when = s.whoop_last_sync_at ? new Date(s.whoop_last_sync_at * 1000).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" }) : "noch nie";
    $("whoop-state").textContent = `Verbunden, zuletzt abgeglichen: ${when}.${s.whoop_error ? ` Problem: ${s.whoop_error}` : ""}`;
  }
}

async function connectWhoop(event) {
  event.preventDefault();
  const error = $("whoop-error");
  error.hidden = true;
  const button = event.submitter;
  button.disabled = true;
  try {
    const result = await api("/api/settings/whoop", { method: "POST", body: { client_id: $("set-whoop-id").value.trim(), client_secret: $("set-whoop-secret").value.trim() } });
    location.href = result.url;
  } catch (problem) {
    error.textContent = problem.message;
    error.hidden = false;
    button.disabled = false;
  }
}

async function removeWhoop() {
  if (!confirm("Verbindung zu WHOOP trennen? Der Zugriff wird bei WHOOP entzogen und die Zugangsdaten vom Server gelöscht. Bereits geladene Werte bleiben erhalten.")) return;
  await api("/api/settings/whoop", { method: "DELETE" });
  await openSettings();
  await loadOverview();
}

function renderClaudeState(s) {
  $("claude-form").hidden = !s.claude_own_key;
  $("set-claude-key").value = "";
  $("claude-error").hidden = true;
  $("claude-remove").hidden = !s.claude_key;
  $("claude-help").open = !s.claude_key;
  $("claude-state").textContent = s.claude_key
    ? `Hinterlegt: ${s.claude_key}. Trage nur dann einen neuen ein, wenn du ihn ersetzen willst.`
    : "Noch kein Key hinterlegt. Ohne Key bleiben Trainer und Auswertungen aus, alles andere funktioniert.";
}

async function saveClaudeKey(event) {
  event.preventDefault();
  const error = $("claude-error");
  error.hidden = true;
  const button = event.submitter;
  button.disabled = true;
  try {
    await api("/api/settings/claude-key", { method: "POST", body: { key: $("set-claude-key").value.trim() } });
    await openSettings();
    await loadOverview();
  } catch (problem) {
    error.textContent = problem.message;
    error.hidden = false;
  } finally {
    button.disabled = false;
  }
}

async function removeClaudeKey() {
  if (!confirm("API-Key entfernen? Trainer und Auswertungen laufen dann nicht mehr.")) return;
  await api("/api/settings/claude-key", { method: "DELETE" });
  await openSettings();
  await loadOverview();
}

async function openSettings() {
  const s = await api("/api/settings");
  $("set-race-name").value = s.race_name;
  $("set-race-date").value = s.race_date;
  $("set-notes").value = s.athlete_notes;
  $("set-athlete-name").value = s.athlete_name;
  $("set-key").value = "";
  $("key-error").hidden = true;
  $("key-state").textContent = s.garmin_key_set ? "Ein Key ist hinterlegt. Trage nur dann einen neuen ein, wenn du ihn ersetzen willst." : "Noch kein Key hinterlegt.";
  $("backup-state").textContent = s.last_backup_at
    ? `Letzte Sicherung: ${new Date(s.last_backup_at * 1000).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" })} Uhr, ${s.backup_count} auf dem Server. Sie läuft jede Nacht.`
    : "Noch keine Sicherung vorhanden.";
  $("set-calendar").value = "";
  $("set-calendar").required = !s.calendar_count;
  $("calendar-terms").hidden = !s.calendar_count;
  $("terms-training").value = s.calendar_training_terms;
  $("terms-ignore").value = s.calendar_ignore_terms;
  $("terms-state").textContent = "";
  $("calendar-unhide").hidden = !s.calendar_hidden;
  $("calendar-unhide").textContent = `${s.calendar_hidden} ausgeblendete Einheiten wieder anzeigen`;
  $("calendar-error").hidden = true;
  $("calendar-remove").hidden = !s.calendar_count;
  renderCalendarState(s);
  renderWhoopState(s);
  renderClaudeState(s);
  $("key-remove").hidden = !s.garmin_key_set;
  $("key-expiry-box").hidden = !s.garmin_key_set;
  $("set-key-expiry").value = s.garmin_key_expires || "";
  $("key-expiry-state").textContent = "";
  if (!$("settings-dialog").open) $("settings-dialog").showModal();
}

async function saveSettings(event) {
  event.preventDefault();
  await api("/api/settings", { method: "PUT", body: { race_name: $("set-race-name").value, race_date: $("set-race-date").value, athlete_notes: $("set-notes").value, athlete_name: $("set-athlete-name").value } });
  $("settings-dialog").close();
  await loadOverview();
}

async function saveKey(event) {
  event.preventDefault();
  const error = $("key-error");
  error.hidden = true;
  const submit = event.submitter;
  submit.disabled = true;
  try {
    const days = $("set-key-days").value;
    await api("/api/settings/garmin-key", { method: "POST", body: { key: $("set-key").value, valid_days: days ? Number(days) : null } });
    $("set-key").value = "";
    $("settings-dialog").close();
    await loadOverview();
    watchFirstSync();
  } catch (problem) {
    error.textContent = problem.message;
    error.hidden = false;
  } finally {
    submit.disabled = false;
  }
}

function watchFirstSync() {
  let rounds = 0;
  const tick = async () => {
    await refreshAll();
    if (++rounds < 8) setTimeout(tick, 15000);
  };
  setTimeout(tick, 5000);
}

async function removeKey() {
  if (!confirm("Garmin-Key vom Server entfernen? Bereits geladene Trainings bleiben erhalten.")) return;
  await api("/api/settings/garmin-key", { method: "DELETE" });
  await openSettings();
  await loadOverview();
}

// --- Reiter -------------------------------------------------------------------
// Der Reiter steht im Hash (#kalender, #verlauf, #trainings), damit Neuladen und Zurück funktionieren.
function tabs() {
  return [...document.querySelectorAll('.tabs [role="tab"]')];
}

function selectTab(id, { push = true } = {}) {
  const target = tabs().find((t) => t.id === id) || tabs()[0];
  for (const tab of tabs()) {
    const on = tab === target;
    tab.setAttribute("aria-selected", String(on));
    tab.tabIndex = on ? 0 : -1;
    $(tab.getAttribute("aria-controls")).hidden = !on;
  }
  hideTooltip();
  if (push) {
    const hash = target.dataset.hash;
    history.replaceState(null, "", hash ? `#${hash}` : location.pathname);
  }
  // Diagramme brauchen die echte Breite, die erst im sichtbaren Reiter feststeht.
  if (target.id === "tab-trends") renderTrends();
}

function tabFromHash() {
  const hash = location.hash.slice(1);
  return (tabs().find((t) => t.dataset.hash === hash && hash) || tabs()[0]).id;
}

function updateWeekMore() {
  const box = $("week-analysis");
  const clamped = box.classList.contains("clamped");
  $("week-more").hidden = !clamped || box.scrollHeight <= box.clientHeight + 4;
}

// --- Ablauf ----------------------------------------------------------------
async function loadOverview() {
  renderOverview(await api("/api/overview"));
}

async function refreshAll() {
  await loadOverview();
  await Promise.all([loadCalendar(), loadRecent(), loadTrends(), loadToday(), loadStrip()]);
}

async function loadTrends() {
  trendData = await api("/api/trends?weeks=52");
  renderTrends();
}

function shiftMonth(delta) {
  state.month = new Date(state.month.getFullYear(), state.month.getMonth() + delta, 1);
  loadCalendar().catch((e) => showBanner(e.message));
}

function wire() {
  $("calendar-form").addEventListener("submit", saveCalendar);
  $("whoop-form").addEventListener("submit", connectWhoop);
  $("claude-form").addEventListener("submit", saveClaudeKey);
  $("claude-remove").addEventListener("click", removeClaudeKey);
  $("whoop-remove").addEventListener("click", removeWhoop);
  $("terms-save").addEventListener("click", async () => {
    try {
      await api("/api/settings/calendar-terms", { method: "PUT", body: { training: $("terms-training").value, ignore: $("terms-ignore").value } });
      $("terms-state").textContent = "Gespeichert und neu eingelesen.";
      await refreshAll();
    } catch (error) {
      $("terms-state").textContent = error.message;
    }
  });
  $("calendar-unhide").addEventListener("click", async () => {
    await api("/api/settings/calendar-unhide", { method: "POST" });
    await openSettings();
    await refreshAll();
  });
  $("calendar-remove").addEventListener("click", removeCalendar);
  $("complaint-new").addEventListener("click", () => openComplaint());
  $("complaint-form").addEventListener("submit", saveComplaint);
  $("complaint-delete").addEventListener("click", deleteComplaint);
  $("key-expiry-save").addEventListener("click", async () => {
    try {
      await api("/api/settings/garmin-key-expiry", { method: "PUT", body: { expires_on: $("set-key-expiry").value || null } });
      $("key-expiry-state").textContent = "Gespeichert.";
      await loadOverview();
    } catch (error) {
      $("key-expiry-state").textContent = error.message;
    }
  });
  $("today-add").addEventListener("click", () => openDay(state.today));
  for (const tab of tabs()) tab.addEventListener("click", () => selectTab(tab.id));
  $("p-home").closest("main").querySelector(".tabs").addEventListener("keydown", (event) => {
    if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
    const list = tabs();
    const next = list[(list.findIndex((t) => t.getAttribute("aria-selected") === "true") + (event.key === "ArrowRight" ? 1 : list.length - 1)) % list.length];
    selectTab(next.id);
    next.focus();
  });
  addEventListener("hashchange", () => selectTab(tabFromHash(), { push: false }));
  document.querySelectorAll("[data-goto]").forEach((button) => button.addEventListener("click", () => selectTab(button.dataset.goto)));
  $("strip-prev").addEventListener("click", () => shiftStrip(-1));
  $("strip-next").addEventListener("click", () => shiftStrip(1));
  $("strip-today").addEventListener("click", () => shiftStrip(0));
  $("recent-more").addEventListener("click", () => loadMoreRecent().catch((e) => showBanner(e.message)));
  $("week-more").addEventListener("click", () => { $("week-analysis").classList.remove("clamped"); updateWeekMore(); });
  new MutationObserver(updateWeekMore).observe($("week-analysis"), { childList: true, subtree: true });
  $("today-ask").addEventListener("click", askToday);
  $("trainer-knowledge").addEventListener("click", () => openKnowledge().catch((error) => { $("chat-error").textContent = error.message; $("chat-error").hidden = false; }));
  $("knowledge-files").addEventListener("change", uploadKnowledge);
  $("instructions-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("/api/knowledge/instructions", { method: "PUT", body: { instructions: $("knowledge-instructions").value } });
      $("instructions-state").textContent = "Gespeichert. Der Trainer nutzt das ab der nächsten Nachricht.";
      await loadKnowledge();
    } catch (error) {
      knowledgeProblem(error.message);
    }
  });
  $("knowledge-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    knowledgeProblem("");
    try {
      await api("/api/knowledge", { method: "POST", body: { title: $("knowledge-title").value, content: $("knowledge-content").value } });
      $("knowledge-title").value = "";
      $("knowledge-content").value = "";
      await loadKnowledge();
    } catch (error) {
      knowledgeProblem(error.message);
    }
  });
  $("trainer-open").addEventListener("click", () => setTrainerOpen(true));
  $("trainer-close").addEventListener("click", () => setTrainerOpen(false));
  $("trainer-clear").addEventListener("click", () => chatAction(async () => {
    if (!chat.messages.length || !confirm("Diesen Chat löschen und neu anfangen? Dein Kalender bleibt, wie er ist.")) return;
    await api("/api/chat", { method: "DELETE" });
    await loadChat();
  }));
  $("chat-form").addEventListener("submit", (event) => { event.preventDefault(); sendChat($("chat-input").value); });
  $("chat-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      sendChat($("chat-input").value);
    }
  });
  segmented("weekly-sport", WEEKLY_SPORTS, "sport");
  segmented("weekly-metric", WEEKLY_METRICS, "metric");
  segmented("weekly-range", WEEKLY_RANGES, "range");
  $("manual-sport").replaceChildren(...STACK.map((s) => el("option", { value: s, textContent: SPORT_NAMES[s] })));
  $("btn-add-activity").addEventListener("click", () => openManual());
  $("day-add-activity").addEventListener("click", () => openManual(null, state.dayDate > state.today ? state.today : state.dayDate));
  $("manual-form").addEventListener("submit", saveManual);
  $("activity-edit").addEventListener("click", async () => {
    await saveNote();
    const activity = await api(`/api/activities/${state.activityId}`);
    $("activity-dialog").close();
    openManual(activity);
  });
  $("activity-delete").addEventListener("click", deleteManual);
  $("activity-note").addEventListener("change", saveNoteQuietly);
  $("plan-sport").replaceChildren(...["swim", "bike", "run", "brick", "strength", "rest", "other"].map((s) => el("option", { value: s, textContent: SPORT_NAMES[s] })));
  $("prev-month").addEventListener("click", () => shiftMonth(-1));
  $("next-month").addEventListener("click", () => shiftMonth(1));
  $("this-month").addEventListener("click", () => {
    const today = parse(state.today);
    state.month = new Date(today.getFullYear(), today.getMonth(), 1);
    loadCalendar().catch((e) => showBanner(e.message));
  });
  $("plan-form").addEventListener("submit", savePlan);
  $("plan-delete").addEventListener("click", deletePlan);
  $("plan-cancel").addEventListener("click", () => resetPlanForm(state.dayDate));
  $("activity-analyze").addEventListener("click", analyzeActivity);
  $("btn-week").addEventListener("click", analyzeWeek);
  $("prev-week").addEventListener("click", () => { state.weekStart = iso(addDays(parse(state.weekStart), -7)); $("week-analysis").classList.add("clamped"); loadWeek(); });
  $("next-week").addEventListener("click", () => { state.weekStart = iso(addDays(parse(state.weekStart), 7)); $("week-analysis").classList.add("clamped"); loadWeek(); });
  $("btn-settings").addEventListener("click", openSettings);
  $("settings-form").addEventListener("submit", saveSettings);
  $("key-form").addEventListener("submit", saveKey);
  $("key-remove").addEventListener("click", removeKey);
  $("btn-logout").addEventListener("click", async () => {
    await fetch("/api/logout", { method: "POST" });
    location.href = "/login";
  });
  $("btn-sync").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    if (state.overview && !state.overview.garmin.connected) {
      openSettings();
      return;
    }
    button.disabled = true;
    $("sync-status").textContent = "gleiche ab …";
    try {
      await api("/api/sync", { method: "POST" });
      button.disabled = false;
      await refreshAll();
    } catch (error) {
      showBanner(error.message);
    } finally {
      button.disabled = false;
      renderSyncPill();
    }
  });
  document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => button.closest("dialog").close()));
  document.querySelectorAll("dialog").forEach((dialog) => dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); }));
  $("activity-dialog").addEventListener("close", () => {
    clearTimeout(timers.activity);
    saveNoteQuietly();
  });
  // Auf Touch-Geräten schließt ein Tipp außerhalb des Diagramms den Tooltip.
  document.addEventListener("pointerdown", (event) => { if (!event.target.closest(".hit")) hideTooltip(); });
  let resizeTimer;
  addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(renderTrends, 150); });
}

async function start() {
  wire();
  selectTab(tabFromHash(), { push: false });
  renderSportLegend();
  await loadOverview();
  const today = parse(state.today);
  state.month = new Date(today.getFullYear(), today.getMonth(), 1);
  state.weekStart = state.currentWeek;
  state.stripWeek = state.currentWeek;
  await Promise.all([loadCalendar(), loadRecent(), loadTrends(), loadWeek(), loadToday(), loadKnowledge(), loadComplaints(), loadStrip()]);
  setInterval(() => { if (!document.hidden && !document.querySelector("dialog[open]")) refreshAll().catch(() => {}); }, 300000);
}

start().catch((error) => showBanner(error.message));
