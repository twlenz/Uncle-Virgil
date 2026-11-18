"use strict";

// --- helpers ---------------------------------------------------------------------------

/** Build a DOM element. Text always goes in as text nodes, never as HTML. */
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "class") node.className = v;
    else if (k in node && k !== "list") node[k] = v;
    else node.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) if (c != null && c !== false) node.append(c);
  return node;
}

async function api(path, { method = "GET", body, form } = {}) {
  const opts = { method, headers: {} };
  if (form) opts.body = form;
  else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
  const r = await fetch("/api" + path, opts);
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : null;
  if (!r.ok) throw new Error(data?.detail || `Request failed (${r.status})`);
  return data;
}

const $ = (id) => document.getElementById(id);
const clock = (iso) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
const clockSec = (iso) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
const longDate = (d) => new Date(d + "T12:00").toLocaleDateString([], { weekday: "long", month: "long", day: "numeric", year: "numeric" });
const dueText = (iso) => new Date(iso).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const pad = (n) => String(n).padStart(2, "0");
const isoDate = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const localInput = (d) => `${isoDate(d)}T${pad(d.getHours())}:${pad(d.getMinutes())}`; // value for <input type=datetime-local>
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const megabytes = (b) => (b >= 1e9 ? (b / 1e9).toFixed(2) + " GB" : (b / 1e6).toFixed(1) + " MB");
function duration(seconds) {
  const m = Math.round(seconds / 60);
  if (m < 1) return "under 1m";
  return m >= 60 ? `${Math.floor(m / 60)}h ${pad(m % 60)}m` : `${m}m`;
}

/** Run an action from a button: disable it meanwhile, show the outcome next to it. */
async function act(button, fn) {
  const holder = button.parentElement;
  const msg = holder.querySelector(":scope > .act-msg") || holder.appendChild(el("span", { class: "act-msg small" }));
  msg.textContent = ""; msg.className = "act-msg small";
  button.disabled = true;
  try {
    const text = await fn();
    if (typeof text === "string") { msg.textContent = text; msg.classList.add("ok-text"); }
  } catch (e) {
    msg.textContent = e.message; msg.classList.add("error-text");
  } finally { button.disabled = false; }
}

// One shared dialog. `onClose` runs exactly once when it closes, however that happens
// (a button, Escape, or another dialog replacing it).
let dialogOnClose = null;
function closeDialog() {
  const d = $("dialog"), f = dialogOnClose;
  dialogOnClose = null;
  if (d.open) d.close();
  if (f) f();
}
$("dialog").addEventListener("cancel", (e) => { e.preventDefault(); closeDialog(); });

function showDialog(content, onClose) {
  closeDialog();
  const d = $("dialog");
  d.replaceChildren(...[content].flat(Infinity).filter((c) => c != null && c !== false));
  d.showModal();
  dialogOnClose = onClose || null;
  return d;
}

const buttons = (...items) => el("div", { class: "row end", style: "margin-top:14px" }, items);
const closeButton = (label = "Close") => el("button", { class: "btn", onclick: closeDialog }, label);
const message = (title, text) => showDialog([el("h2", {}, title), el("p", {}, text), buttons(closeButton())]);

/** Ask before something that cannot be undone. Resolves true or false. */
function confirmDialog(title, text, actionLabel) {
  return new Promise((resolve) => {
    showDialog([el("h2", {}, title), el("p", {}, text),
      buttons(closeButton("Cancel"), el("button", { class: "btn danger", onclick: () => { resolve(true); closeDialog(); } }, actionLabel))],
      () => resolve(false));
  });
}

/** Text with each search word highlighted. */
function highlight(text, words) {
  const pattern = words.filter(Boolean).map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|");
  if (!pattern) return [text];
  return text.split(new RegExp(`(${pattern})`, "gi")).map((part, i) => (i % 2 ? el("mark", {}, part) : part));
}

// --- state and navigation ------------------------------------------------------------------

const state = { view: "today", date: null, tab: null, focus: null, query: "", status: null, lastPending: 0, today: isoDate(new Date()), renderedKey: "" };
const NAV = [["today", "Today"], ["yesterday", "Yesterday"], ["history", "History"], ["reminders", "Reminders"], ["people", "People"], ["storage", "Storage"], ["settings", "Settings"]];
const DAY_VIEWS = ["today", "yesterday", "day"];

function go(view, date, { tab = null, focus = null } = {}) {
  Object.assign(state, { view, date: date || null, tab, focus });
  $("nav").replaceChildren(...NAV.map(([key, label]) => el("button", { class: key === view ? "active" : "", onclick: () => go(key) }, label)));
  render();
}

function viewedDate() {
  if (state.view === "today") return state.today;
  if (state.view === "yesterday") { const d = new Date(state.today + "T12:00"); d.setDate(d.getDate() - 1); return isoDate(d); }
  return state.date;
}

let renderTicket = 0;
async function render() {
  const view = $("view");
  const key = `${state.view}|${viewedDate()}|${state.tab}`;
  const keepScroll = key === state.renderedKey ? view.scrollTop : 0; // a refresh of the same page stays where you were
  const ticket = ++renderTicket;
  let nodes;
  try {
    nodes = await (DAY_VIEWS.includes(state.view) ? renderDay(viewedDate()) : PAGES[state.view]());
  } catch (e) {
    nodes = [el("p", { class: "error-text" }, e.message)];
  }
  if (ticket !== renderTicket) return; // a newer render has started; let it win
  view.replaceChildren(...[nodes].flat(Infinity).filter((n) => n != null && n !== false));
  state.renderedKey = key;
  view.scrollTop = keepScroll;
  renderSetup();
  markPlaying();
  if (state.focus) {
    const target = view.querySelector(`[data-id="${CSS.escape(state.focus)}"]`) || $(`settings-${state.focus}`);
    if (target) { target.scrollIntoView({ block: "center" }); target.classList.add("focus"); }
    state.focus = null;
  }
}

// --- recording bar, banners and status --------------------------------------------------------

async function pollStatus() {
  let s;
  try { s = await api("/status"); } catch { $("recstate").textContent = "Not connected to the Uncle Virgil service"; return; }
  state.status = s;
  const dictating = s.mode === "dictation";
  $("dictate").textContent = dictating ? "Stop dictation" : "Dictate";
  $("dictate").classList.toggle("recording", dictating);
  $("dictate").classList.toggle("primary", !dictating);
  $("dictate").disabled = s.mode === "sample";
  $("dictate").title = (dictating ? "Stop and save the dictation" : "Record until you press again") + (s.hotkey ? ` (or press ${s.hotkey} in any program)` : "");
  $("continuous").checked = s.continuous;
  $("level").style.width = s.mode === "idle" ? "0" : Math.round(s.level * 100) + "%";
  $("recstate").textContent = s.recorder_error || { dictation: "Recording… press Stop dictation when you are done", sample: "Recording voice sample…", continuous: s.speaking ? "Hearing speech" : "Listening", idle: "" }[s.mode];
  $("recstate").classList.toggle("error-text", !!s.recorder_error);
  $("foot").textContent = s.pending ? `Transcribing ${plural(s.pending, "recording")}…` : "";
  renderBanners(s);
  renderSetup();

  const newDay = s.today !== state.today;
  state.today = s.today;
  const finished = s.pending < state.lastPending;
  state.lastPending = s.pending;
  if ($("dialog").open || !DAY_VIEWS.includes(state.view)) return;
  // Follow the calendar if the app is left open overnight, and show new transcripts as they arrive
  // (on any day: an imported recording can belong to an earlier one).
  if ((newDay && state.view !== "day") || finished) render();
}

function renderBanners(s) {
  const m = s.models;
  const key = JSON.stringify([s.disk_low, s.disk_low && s.disk_free_gb, s.damaged_files, m.progress.active, m.progress.error, m.ready, s.pipeline_error, s.alerts.map((r) => r.id), s.heard.map((r) => [r.id, r.due, r.text])]);
  if (key === renderBanners.key) {
    if (m.progress.active) {
      $("banners").querySelector("progress").value = m.progress.done_mb;
      $("banners").querySelector(".progress-text").textContent = `Downloading ${m.progress.model}… ${Math.round(m.progress.done_mb)} of ${Math.round(m.progress.total_mb)} MB`;
    }
    return; // rebuild only on change, so buttons are not replaced mid-click
  }
  renderBanners.key = key;
  const out = [];
  if (m.progress.active) {
    out.push(el("div", { class: "banner" }, el("span", { class: "grow progress-text" }, `Downloading ${m.progress.model}…`),
      el("progress", { max: m.progress.total_mb || 1, value: m.progress.done_mb })));
  } else if (!m.ready) {
    const size = m.missing.reduce((a, x) => a + x.size_mb, 0);
    out.push(el("div", { class: "banner alert" },
      el("span", { class: "grow" }, m.progress.error ? `The model download failed: ${m.progress.error}` : `The speech models are not installed yet (a one-time download of about ${size} MB). Anything you record is kept and transcribed once they are.`),
      el("button", { class: "btn primary", onclick: (e) => act(e.target, () => api("/models/download", { method: "POST" })) }, m.progress.error ? "Try again" : "Download models")));
  }
  if (s.disk_low) out.push(el("div", { class: "banner error" }, el("span", { class: "grow" },
    `Low disk space: ${s.disk_free_gb} GB free on the drive that holds your recordings. Recording stops below 1 GB, so the drive never fills up.`),
    el("button", { class: "btn", onclick: () => go("storage") }, "Free up space")));
  if (s.damaged_files.length) out.push(el("div", { class: "banner error" }, el("span", { class: "grow" },
    `Some files could not be read and were kept aside under a new name, so nothing was lost: ${s.damaged_files.join("; ")}. The app carried on without them.`)));
  if (s.pipeline_error) out.push(el("div", { class: "banner error" }, el("span", { class: "grow" }, "A recording could not be transcribed: " + s.pipeline_error)));
  for (const r of s.heard) {
    out.push(el("div", { class: "banner info" }, el("span", { class: "grow" }, el("strong", {}, "Reminder set: "), `${r.text}, ${dueText(r.due)}`),
      el("button", { class: "btn", onclick: () => editReminder(r) }, "Change"),
      el("button", { class: "btn", onclick: async () => { await api(`/reminders/${r.id}`, { method: "PATCH", body: { acknowledged: true } }); pollStatus(); } }, "OK")));
  }
  for (const r of s.alerts) {
    out.push(el("div", { class: "banner alert" }, el("span", { class: "grow" }, el("strong", {}, "Reminder: "), `${r.text} (${clock(r.due)})`),
      el("button", { class: "btn", onclick: async () => { await api(`/reminders/${r.id}`, { method: "PATCH", body: { snooze_minutes: 10 } }); pollStatus(); } }, "Snooze 10 min"),
      el("button", { class: "btn", onclick: async () => { await api(`/reminders/${r.id}`, { method: "PATCH", body: { status: "done" } }); pollStatus(); } }, "Done")));
  }
  $("banners").replaceChildren(...out);
}

$("dictate").addEventListener("click", async () => {
  try { await api("/record/dictation", { method: "POST", body: { on: state.status?.mode !== "dictation" } }); }
  catch (e) { message("Could not record", e.message); }
  pollStatus();
});
$("continuous").addEventListener("change", async (e) => {
  try { await api("/record/continuous", { method: "POST", body: { on: e.target.checked } }); }
  catch (err) { e.target.checked = false; message("Could not start continuous recording", err.message); }
  pollStatus();
});

// --- getting started ---------------------------------------------------------------------------

function setupSteps(s) {
  const m = s.models;
  return [
    { done: m.ready, text: "Download the speech models (one time).",
      action: m.progress.active ? `Downloading… ${Math.round(m.progress.done_mb)} of ${Math.round(m.progress.total_mb)} MB` : ["Download", (b) => act(b, () => api("/models/download", { method: "POST" }))] },
    { done: s.owner_enrolled, text: "Record a 20-second voice sample, so your lines are labeled with your name and reminders only come from your own voice.",
      action: ["Go to People", () => go("people")] },
    { done: s.llm_provider !== "none", text: "Optional: connect a language model, so the daily summary can sort your day into tasks.",
      action: ["Open Settings", () => go("settings", null, { focus: "llm" })] },
    { done: s.has_reminders, text: "Try a reminder: press Dictate, say “note to self, stretch in 10 minutes”, then press Stop dictation.", action: null },
  ];
}

function renderSetup() {
  const box = $("setup"), s = state.status;
  if (!box || !s) return;
  const steps = setupSteps(s);
  const show = !s.setup_dismissed && steps.some((x) => !x.done);
  const key = JSON.stringify([show, steps.map((x) => [x.done, typeof x.action === "string" ? x.action : ""])]);
  if (key === box.dataset.key) return;
  box.dataset.key = key;
  if (!show) { box.replaceChildren(); return; }
  box.replaceChildren(el("div", { class: "card setup" },
    el("div", { class: "row spread" }, el("h2", {}, "Getting started"),
      el("button", { class: "btn link small", onclick: async () => { await api("/settings", { method: "PUT", body: { setup_dismissed: true } }); pollStatus(); } }, "Hide this")),
    el("ol", {}, steps.map((x) => el("li", { class: x.done ? "done" : "" },
      el("span", { class: "tick", "aria-hidden": "true" }, x.done ? "✓" : ""),
      el("span", { class: "grow" }, x.text),
      !x.done && x.action && (typeof x.action === "string" ? el("span", { class: "muted small" }, x.action)
        : el("button", { class: "btn", onclick: (e) => x.action[1](e.target) }, x.action[0])))))));
}

// --- day view --------------------------------------------------------------------------------

async function renderDay(date) {
  const day = await api(`/days/${date}`);
  const hasText = day.conversations.length > 0;
  const tab = state.tab || (day.summary ? "summary" : "transcript");
  const fileInput = el("input", { type: "file", accept: "audio/*,.flac,.opus,.m4a,.wav,.mp3,.ogg", style: "display:none",
    onchange: (e) => { const f = e.target.files[0]; e.target.value = ""; if (f) importDialog(f); } });
  const head = el("div", { class: "row spread" },
    el("div", {}, el("h1", {}, state.view === "day" ? longDate(date) : state.view === "today" ? "Today" : "Yesterday"),
      state.view !== "day" && el("div", { class: "muted" }, longDate(date))),
    el("div", { class: "row" }, fileInput,
      el("button", { class: "btn", title: "Add a recording made elsewhere, such as on a phone", onclick: () => fileInput.click() }, "Import audio…"),
      el("button", { class: "btn", title: "Show this day's files", onclick: (e) => act(e.target, () => api("/open-folder", { method: "POST", body: { date } }).then(() => undefined)) }, "Open folder")));
  const waiting = day.untranscribed > 0 && el("p", { class: "note waiting" },
    `${plural(day.untranscribed, "recording")} from this day ${day.untranscribed === 1 ? "is" : "are"} waiting to be transcribed` +
    (state.status?.models.ready ? ". This happens in the background." : ". Transcription starts once the speech models are downloaded."));
  const noSpeech = day.no_speech > 0 && el("p", { class: "note waiting" },
    `${plural(day.no_speech, "recording")} from this day had no words the recognizer could make out (for example background noise or very quiet speech). The audio is kept in this day's folder under audio\\no-speech.`);
  const tabs = el("div", { class: "tabs", role: "tablist" }, ["summary", "transcript"].map((t) =>
    el("button", { class: t === tab ? "active" : "", role: "tab", onclick: () => { state.tab = t; render(); } }, t === "summary" ? "Summary" : "Transcript")));
  const empty = state.view === "today"
    ? el("div", { class: "empty" }, el("p", {}, "Nothing recorded yet today."), el("p", { class: "muted" }, "Press Dictate to record a note, or switch on Continuous recording to capture conversations as they happen. Text appears here shortly after each pause."))
    : el("p", { class: "muted" }, "Nothing was transcribed on this day.");
  const body = !hasText ? empty : tab === "summary" ? summaryPanel(day) : transcriptPanel(day);
  if (day.job?.state === "running") setTimeout(() => { if (viewedDate() === date && DAY_VIEWS.includes(state.view)) render(); }, 2000);
  return [head, state.view === "today" && el("div", { id: "setup" }), waiting, noSpeech, tabs, body];
}

function importDialog(file) {
  const when = el("input", { type: "datetime-local", value: localInput(new Date(file.lastModified)), class: "auto" });
  const which = el("select", { "aria-label": "Started or ended", class: "auto" }, el("option", { value: "ended", selected: true }, "ended at"), el("option", { value: "started" }, "started at"));
  const start = el("button", { class: "btn primary", onclick: (e) => act(e.target, async () => {
    if (!when.value) throw new Error("Choose when the recording was made");
    e.target.textContent = "Importing…";
    const form = new FormData();
    form.append("file", file);
    form.append(which.value, when.value);
    let r;
    try { r = await api("/import", { method: "POST", form }); } finally { e.target.textContent = "Import"; }
    showDialog([el("h2", {}, "Recording added"),
      el("p", {}, r.blocks ? `${duration(r.seconds)} of audio was added to ${longDate(r.date)} as ${plural(r.blocks, "recording")}, and is being transcribed in the background.`
        : "No speech was found in that file, so nothing was added."),
      buttons(closeButton(), r.blocks > 0 && r.date !== viewedDate() && el("button", { class: "btn primary", onclick: () => { closeDialog(); go("day", r.date, { tab: "transcript" }); } }, "Open that day"))]);
    pollStatus();
  }) }, "Import");
  showDialog([el("h2", {}, "Add a recording"), el("p", { class: "muted" }, file.name),
    el("div", { class: "row" }, el("span", {}, "The recording"), which, when),
    el("p", { class: "note" }, "Filled in from the file's date, which most recorders set when a recording ends. Correct it if you know better; it sets the times in the transcript."),
    buttons(closeButton("Cancel"), start)]);
}

function summaryPanel(day) {
  const s = day.summary;
  const running = day.job?.state === "running";
  const generate = el("button", { class: "btn" + (s ? "" : " primary"), disabled: running, onclick: async () => { await api(`/days/${day.date}/summarize`, { method: "POST" }); render(); } },
    running ? "Summarizing…" : s ? "Regenerate" : "Make summary now");
  const noLlm = day.llm === "none" && el("p", { class: "note" }, "No language model is connected, so the summary lists notes and reminders but cannot sort the day into tasks. ",
    el("button", { class: "btn link", onclick: () => go("settings", null, { focus: "llm" }) }, "Connect one in Settings"));
  const failed = day.job?.state === "error" && el("p", { class: "error-text" }, "The last summary attempt failed: " + day.job.error);
  const auto = state.status && day.date === state.today ? " One is made automatically in the evening (see Settings)." : "";
  if (!s) return el("div", { class: "card" }, el("p", {}, "No summary has been made for this day yet." + auto), noLlm, failed, el("div", { class: "row" }, generate));

  const t = s.totals;
  const noteTime = (n) => (/^\d\d:\d\d$/.test(n.time) ? clock(`${day.date}T${n.time}`) : n.time);
  const out = [el("div", { class: "card" },
    el("p", { class: "muted" }, `${plural(t.conversations, "conversation")} between ${clock(t.first)} and ${clock(t.last)}, ${duration(t.speech_seconds)} of speech.`),
    s.overview && el("p", {}, s.overview), !s.llm && noLlm, failed,
    el("div", { class: "row small muted" }, generate, `Made ${new Date(s.generated_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}` +
      (s.llm ? ` with ${s.llm.model || s.llm.provider}${s.llm.off_device ? " (online service)" : " (on this computer)"}` : " without a language model")))];

  if (s.tasks.length) {
    out.push(el("div", { class: "card" }, el("h2", {}, "Work performed"), s.tasks.map((task) => [
      el("h3", {}, task.task), el("p", {}, task.summary),
      task.subtasks.some((x) => x.summary) && el("ul", {}, task.subtasks.filter((x) => x.summary).map((x) => el("li", {}, el("strong", {}, x.subtask + ": "), x.summary)))])));
    const max = Math.max(...s.tasks.map((x) => x.seconds));
    const row = (name, item, sub) => el("tr", { class: sub ? "sub" : "", title: `${name}: ${duration(item.seconds)}, ${Math.round(item.share * 100)}% of tracked time` },
      el("td", { class: "name" }, name),
      el("td", {}, el("div", { class: "track" }, el("div", { class: "bar", style: `width:${(item.seconds / max) * 100}%` }))),
      el("td", { class: "num" }, duration(item.seconds)), el("td", { class: "num share" }, Math.round(item.share * 100) + "%"));
    out.push(el("div", { class: "card" }, el("h2", {}, "Time by task"),
      el("table", { class: "bars" }, el("tbody", {}, s.tasks.map((task) => [row(task.task, task), ...task.subtasks.map((x) => row(x.subtask, x, true))]))),
      el("p", { class: "note" }, `Time is each conversation's length plus up to ${s.gap_attribution_min} minutes of the quiet time after it. Percentages are shares of that tracked time, ${duration(t.tracked_seconds)} in total, not of the whole day.`)));
  }
  out.push(el("div", { class: "card" }, el("h2", {}, "Notes to self and ideas"),
    s.notes.length ? el("ul", { class: "list" }, s.notes.map((n) => el("li", {}, el("span", { class: "muted when" }, noteTime(n)), el("span", { class: "tag" }, n.kind), el("span", { class: "grow" }, n.text))))
      : el("p", { class: "muted" }, "None recorded.")));
  if (s.reminders.length) out.push(el("div", { class: "card" }, el("h2", {}, "Reminders set"),
    el("ul", { class: "list" }, s.reminders.map((r) => el("li", {}, el("span", { class: "grow" }, r.text), el("span", { class: "muted" }, dueText(r.due)))))));
  return el("div", {}, out);
}

function transcriptPanel(day) {
  return el("div", { class: "card transcript" }, day.conversations.map((c) => [
    el("div", { class: "conv-head" }, el("span", {}, `${clock(c.start)} to ${clock(c.end)}`),
      el("button", { class: "btn link subtle small", title: "Delete this conversation and its audio", onclick: () => deleteConversation(day, c) }, "Delete…")),
    c.segments.map((seg) => transcriptLine(day, seg))]));
}

function transcriptLine(day, seg) {
  const unknown = /^unk-\d+$/.test(seg.speaker_id);
  return el("div", { class: "utt", "data-id": seg.id },
    el("time", {}, clockSec(seg.start)),
    el("button", { class: "who" + (unknown ? " unknown" : ""), title: unknown ? "Who is this? Click to name them" : "Change who said this",
      onclick: () => (unknown ? nameSpeaker(day.date, seg) : editLine(day, seg)) }, seg.speaker),
    el("span", { class: "text" }, seg.text),
    el("span", { class: "actions" },
      el("button", { class: "icon edit", title: "Correct this line", "aria-label": "Correct this line", onclick: () => editLine(day, seg) }, "✎"),
      seg.has_audio && el("button", { class: "icon play", title: "Play this line", "aria-label": "Play this line", onclick: () => togglePlay(day.date, seg) }, "▶")));
}

// Playback: one line at a time, stopping at the end of the line. Press again to stop.
const player = $("player");
let playing = null; // { id, end, started }
function togglePlay(date, seg) {
  if (playing?.id === seg.id) { playing = null; player.pause(); markPlaying(); return; }
  const src = `/api/audio/${date}/${seg.block}`;
  const from = seg.offset;
  playing = { id: seg.id, end: from + (new Date(seg.end) - new Date(seg.start)) / 1000 + 0.3, started: false };
  const start = () => {
    player.currentTime = from;
    player.play().catch((e) => {
      if (e.name === "AbortError") return; // replaced by another line
      playing = null; markPlaying(); message("Could not play this recording", e.message);
    });
  };
  if (player.getAttribute("src") !== src) {
    player.addEventListener("loadedmetadata", start, { once: true });
    player.src = src;
  } else start();
  markPlaying();
}
player.addEventListener("playing", () => { if (playing) playing.started = true; });
player.addEventListener("timeupdate", () => { if (playing && player.currentTime >= playing.end) player.pause(); });
for (const ev of ["pause", "ended"]) player.addEventListener(ev, () => { if (playing?.started && player.paused) { playing = null; markPlaying(); } });
player.addEventListener("error", () => { if (playing) { playing = null; markPlaying(); message("Could not play this recording", "The audio file could not be loaded. It may have been deleted."); } });

function markPlaying() {
  for (const line of document.querySelectorAll(".utt")) {
    const on = playing?.id === line.dataset.id;
    line.classList.toggle("playing", on);
    const b = line.querySelector(".play");
    if (b) { b.textContent = on ? "■" : "▶"; b.title = on ? "Stop" : "Play this line"; b.setAttribute("aria-label", b.title); }
  }
}

function editLine(day, seg) {
  const text = el("textarea", { rows: 3, value: seg.text });
  const who = el("select", {}, day.speakers.map((c) => el("option", { value: c.id, selected: c.id === seg.speaker_id }, c.name)));
  if (!day.speakers.some((c) => c.id === seg.speaker_id)) who.prepend(el("option", { value: seg.speaker_id, selected: true }, seg.speaker));
  const save = el("button", { class: "btn primary", onclick: (e) => act(e.target, async () => {
    const body = {};
    if (text.value.trim() !== seg.text) body.text = text.value;
    if (who.value !== seg.speaker_id) body.speaker_id = who.value;
    if (Object.keys(body).length) await api(`/days/${day.date}/segments/${encodeURIComponent(seg.id)}`, { method: "PATCH", body });
    closeDialog(); render();
  }) }, "Save");
  const remove = el("button", { class: "btn danger", onclick: async () => {
    if (!(await confirmDialog("Delete this line?", "The line is permanently deleted, and so is its audio unless another line uses the same recording.", "Delete"))) return;
    try { await api(`/days/${day.date}/segments/delete`, { method: "POST", body: { ids: [seg.id] } }); render(); }
    catch (e) { message("Could not delete", e.message); }
  } }, "Delete line");
  showDialog([el("h2", {}, "Correct this line"), el("p", { class: "muted small" }, `Said at ${clockSec(seg.start)}`),
    el("label", { class: "stack" }, el("span", {}, "Words"), text),
    el("label", { class: "stack" }, el("span", {}, "Said by"), who),
    el("p", { class: "note" }, "To give a new voice a name, close this and click its “Speaker” label in the transcript."),
    el("div", { class: "row spread", style: "margin-top:14px" }, remove, el("div", { class: "row" }, closeButton("Cancel"), save))]);
  text.focus();
}

async function deleteConversation(day, c) {
  const summaryNote = day.summary ? " Regenerate the summary afterwards if this conversation was in it." : "";
  if (!(await confirmDialog("Delete this conversation?", `${plural(c.segments.length, "line")} from ${clock(c.start)} to ${clock(c.end)}, and the audio they came from, will be permanently deleted. Use this for things that should not have been recorded.${summaryNote}`, "Delete"))) return;
  try { await api(`/days/${day.date}/segments/delete`, { method: "POST", body: { ids: c.segments.map((x) => x.id) } }); }
  catch (e) { message("Could not delete", e.message); }
  render();
}

function nameSpeaker(date, seg) {
  const name = el("input", { type: "text", placeholder: "Name" });
  const me = el("input", { type: "checkbox" });
  const save = el("button", { class: "btn primary", onclick: (e) => act(e.target, async () => {
    if (!name.value.trim()) throw new Error("Enter a name");
    await api(`/days/${date}/speakers/${seg.speaker_id}`, { method: "POST", body: { name: name.value, is_user: me.checked } });
    closeDialog(); render();
  }) }, "Save");
  showDialog([el("h2", {}, `Who is ${seg.speaker}?`),
    el("p", { class: "note" }, `Every line from ${seg.speaker} today gets this name, and their voice is recognized by name from now on. The voiceprint stays on this computer; you can remove it under People.`),
    el("label", { class: "field" }, el("span", {}, "Name"), name), el("label", { class: "check" }, me, "This is me"),
    buttons(closeButton("Cancel"), save)]);
  name.focus();
  name.addEventListener("keydown", (e) => { if (e.key === "Enter") save.click(); });
}

// --- history and search ------------------------------------------------------------------------

async function renderHistory() {
  const days = await api("/days");
  const results = el("div", { class: "results" });
  const dayList = days.length
    ? el("ul", { class: "list" }, days.map((d) => el("li", {},
      el("button", { class: "btn link grow", style: "text-align:left", onclick: () => go("day", d.date) }, longDate(d.date)),
      d.has_summary && el("span", { class: "tag" }, "summary"))))
    : el("p", { class: "muted" }, "No days recorded yet.");
  let timer = null;
  const search = el("input", { type: "search", value: state.query, placeholder: "Search everything you've recorded", "aria-label": "Search transcripts",
    oninput: () => { clearTimeout(timer); timer = setTimeout(run, 250); } });
  async function run() {
    state.query = search.value;
    const words = search.value.trim().split(/\s+/).filter(Boolean);
    if (!words.length) { results.replaceChildren(el("h2", {}, "Days"), dayList); return; }
    let hits;
    try { hits = await api(`/search?q=${encodeURIComponent(search.value)}`); } catch (e) { results.replaceChildren(el("p", { class: "error-text" }, e.message)); return; }
    if (search.value !== state.query) return; // typed more meanwhile
    const byDate = Map.groupBy ? Map.groupBy(hits, (h) => h.date) : hits.reduce((m, h) => m.set(h.date, [...(m.get(h.date) || []), h]), new Map());
    results.replaceChildren(...[
      el("p", { class: "muted small" }, hits.length >= 200 ? "Showing the first 200 matches." : hits.length === 1 ? "1 match." : `${hits.length} matches.`),
      [...byDate].map(([date, items]) => [el("h3", {}, longDate(date)),
        el("ul", { class: "list hits" }, items.map((h) => el("li", {},
          el("button", { class: "hit", onclick: () => go("day", date, { tab: "transcript", focus: h.id }) },
            el("span", { class: "muted when" }, clock(h.start)), el("strong", {}, h.speaker + ": "), highlight(h.text, words)))))])].flat(Infinity));
  }
  run();
  return [el("h1", {}, "History"),
    el("div", { class: "card", style: "margin-top:14px" }, el("div", { class: "row" }, search,
      el("label", { class: "row small muted" }, "or go to a day", el("input", { type: "date", max: state.today, onchange: (e) => e.target.value && go("day", e.target.value) }))),
      results)];
}

// --- reminders -------------------------------------------------------------------------------

function editReminder(r) {
  const text = el("input", { type: "text", value: r.text });
  const due = el("input", { type: "datetime-local", value: localInput(new Date(r.due)) });
  const save = el("button", { class: "btn primary", onclick: (e) => act(e.target, async () => {
    await api(`/reminders/${r.id}`, { method: "PATCH", body: { text: text.value, due: due.value } });
    closeDialog(); pollStatus(); if (state.view === "reminders") render();
  }) }, "Save");
  showDialog([el("h2", {}, "Change reminder"),
    r.source !== "manual" && el("p", { class: "note" }, "This was set from something you said. If the time was misheard, correct it here."),
    el("label", { class: "stack" }, el("span", {}, "What"), text), el("label", { class: "stack" }, el("span", {}, "When"), due),
    el("p", { class: "note" }, "You are notified ahead of time, as set in Settings."),
    buttons(closeButton("Cancel"), save)]);
}

async function renderReminders() {
  const all = await api("/reminders");
  const text = el("input", { type: "text", placeholder: "What to be reminded of" });
  const due = el("input", { type: "datetime-local" });
  const add = el("button", { class: "btn primary", onclick: (e) => act(e.target, async () => { await api("/reminders", { method: "POST", body: { text: text.value, due: due.value } }); render(); pollStatus(); }) }, "Add reminder");
  const refresh = () => { render(); pollStatus(); };
  const patch = (r, body) => api(`/reminders/${r.id}`, { method: "PATCH", body }).then(refresh);
  const item = (r) => el("li", {}, el("span", { class: "muted when-wide" }, dueText(r.due)), el("span", { class: "grow" }, r.text),
    el("span", { class: "tag", title: { rule: "Heard in your speech", llm: "Found by the language model", manual: "Added by hand" }[r.source] }, r.source === "manual" ? "typed" : "heard"),
    r.status === "missed" && el("span", { class: "tag", title: "The computer was off or asleep when this was due" }, "missed"),
    r.status !== "done" && el("button", { class: "btn", onclick: () => editReminder(r) }, "Change"),
    r.status === "notified" && el("button", { class: "btn", onclick: () => patch(r, { snooze_minutes: 10 }) }, "Snooze 10 min"),
    r.status !== "done" && el("button", { class: "btn", onclick: () => patch(r, { status: "done" }) }, "Done"),
    el("button", { class: "btn subtle", title: "Delete", onclick: () => api(`/reminders/${r.id}`, { method: "DELETE" }).then(refresh) }, "Delete"));
  const section = (title, items, empty) => el("div", { class: "card" }, el("h2", {}, title), items.length ? el("ul", { class: "list" }, items.map(item)) : el("p", { class: "muted" }, empty));
  return [el("h1", {}, "Reminders"),
    el("p", { class: "note", style: "margin-top:8px" }, "Say “note to self” or “remind me” followed by what and when, for example “note to self, meeting with Jack at 5 pm”. You are notified ahead of time by the methods chosen in Settings."),
    el("div", { class: "card" }, el("div", { class: "row" }, text, due, add)),
    section("Upcoming", all.filter((r) => r.status === "pending"), "Nothing coming up."),
    section("Notified", all.filter((r) => r.status === "notified"), "Nothing waiting."),
    section("Past", all.filter((r) => ["done", "missed"].includes(r.status)).reverse().slice(0, 50), "Nothing yet.")];
}

// --- people ----------------------------------------------------------------------------------

async function renderPeople() {
  const [speakers, cfg] = await Promise.all([api("/speakers"), api("/settings")]);
  const hasOwner = speakers.some((s) => s.is_user);
  const name = el("input", { type: "text", value: hasOwner || cfg.settings.user_name === "Me" ? "" : cfg.settings.user_name, placeholder: "Name" });
  const me = el("input", { type: "checkbox", checked: !hasOwner });
  const start = el("button", { class: "btn primary", onclick: (e) => act(e.target, async () => {
    if (!name.value.trim()) throw new Error("Enter a name first");
    await api("/speakers/sample", { method: "POST", body: { on: true } });
    recordSample(name.value.trim(), me.checked);
  }) }, "Record a voice sample");
  return [el("h1", {}, "People"),
    el("p", { class: "note", style: "margin-top:8px" }, "Voiceprints let transcripts show names. They are stored only on this computer (speakers.json in the data folder). Other voices appear as Speaker 1, 2… and can be named by clicking that label in a transcript."),
    el("div", { class: "card" }, el("h2", {}, hasOwner ? "Add a person" : "Add your voice"),
      el("label", { class: "field" }, el("span", {}, "Name"), name), el("label", { class: "check" }, me, "This is me"),
      el("p", { class: "note" }, "You will read a short passage aloud for about 20 seconds. Adding another sample for the same name improves recognition, for example one recorded in a noisier place."),
      el("div", { class: "row" }, start)),
    el("div", { class: "card" }, el("h2", {}, "Known voices"),
      speakers.length ? el("ul", { class: "list" }, speakers.map((s) => {
        const input = el("input", { type: "text", value: s.name, style: "max-width:220px", "aria-label": "Name" });
        return el("li", {}, input, s.is_user && el("span", { class: "tag" }, "you"), el("span", { class: "muted grow" }, plural(s.samples, "sample")),
          el("button", { class: "btn", onclick: (e) => act(e.target, async () => { await api(`/speakers/${s.id}`, { method: "PATCH", body: { name: input.value } }); return "Renamed"; }) }, "Rename"),
          el("button", { class: "btn danger", onclick: async () => {
            if (await confirmDialog("Forget this voice?", `${s.name}'s voiceprint will be deleted. Existing transcripts keep the name.`, "Forget")) { await api(`/speakers/${s.id}`, { method: "DELETE" }); render(); }
          } }, "Forget"));
      })) : el("p", { class: "muted" }, "Nobody yet."))];
}

function recordSample(name, isUser) {
  const started = Date.now();
  const timer = el("strong", {}, "0s");
  const hint = el("span", { class: "muted small" }, "Keep going until at least 15s");
  const tick = setInterval(() => {
    const s = Math.round((Date.now() - started) / 1000);
    timer.textContent = s + "s";
    if (s >= 15) hint.textContent = "That is enough; you can stop now";
  }, 500);
  let finished = false;
  const stop = (save) => api("/speakers/sample", { method: "POST", body: { on: false, name: save ? name : "", is_user: isUser } });
  const save = el("button", { class: "btn primary", onclick: (e) => act(e.target, async () => {
    finished = true; clearInterval(tick);
    try { await stop(true); } catch (err) { showDialog([el("h2", {}, "Voice sample not saved"), el("p", {}, err.message), buttons(closeButton())]); return; }
    closeDialog(); render(); pollStatus();
  }) }, "Stop and save");
  showDialog([el("h2", {}, "Recording… ", timer), el("p", {}, "Read this aloud in your normal voice:"),
    el("div", { class: "read-aloud" }, "Uncle Virgil keeps a private record of my day. It listens for the things I say, writes them down with the time, and reminds me of what I ask it to remember. Everything it hears stays on my own computer unless I decide otherwise. When I say “note to self”, it saves what follows as a note."),
    el("div", { class: "row spread", style: "margin-top:14px" }, hint, el("div", { class: "row" }, closeButton("Cancel"), save))],
    () => { if (!finished) { finished = true; clearInterval(tick); stop(false).catch(() => {}); } }); // cancelled: discard the sample
}

// --- storage ---------------------------------------------------------------------------------

async function renderStorage() {
  const [days, cfg] = await Promise.all([api("/days"), api("/settings")]);
  const today = state.today;
  const start = el("input", { type: "date", value: days.length ? days[days.length - 1].date : today, max: today, "aria-label": "From" });
  const end = el("input", { type: "date", value: today, max: today, "aria-label": "To" });
  const usage = el("p", {}, "Checking…");
  const range = () => ({ start: start.value, end: end.value });
  const refresh = async () => {
    try {
      const u = await api(`/storage/usage?start=${start.value}&end=${end.value}`);
      usage.textContent = u.files ? `${plural(u.files, "audio file")} in this range: ${megabytes(u.bytes)}, ${duration(u.seconds)} of recordings, ${u.compressed_files} already compressed.` : "No audio in this range.";
    } catch (e) { usage.textContent = e.message; }
  };
  start.onchange = end.onchange = refresh;
  const compress = el("button", { class: "btn", onclick: (e) => act(e.target, async () => {
    await api("/storage/compress", { method: "POST", body: range() });
    for (;;) {
      await new Promise((r) => setTimeout(r, 700));
      const job = await api("/jobs/compress");
      if (job.state === "error") throw new Error(job.error);
      if (job.state === "done") { refresh(); return job.result.files ? `Compressed ${plural(job.result.files, "file")}: ${megabytes(job.result.bytes_before)} down to ${megabytes(job.result.bytes_after)}.` : "Nothing left to compress in this range."; }
    }
  }) }, "Compress audio in range");
  const remove = el("button", { class: "btn danger", onclick: async (e) => {
    if (!(await confirmDialog("Delete audio?", `All audio recorded from ${longDate(start.value)} to ${longDate(end.value)} will be permanently deleted. Transcripts and summaries are kept.`, "Delete audio"))) return;
    act(e.target, async () => { const r = await api("/storage/delete", { method: "POST", body: range() }); refresh(); return `Deleted ${plural(r.files, "file")}, freeing ${megabytes(r.bytes)}.`; });
  } }, "Delete audio in range, keep text");
  refresh();
  return [el("h1", {}, "Storage"),
    el("div", { class: "card", style: "margin-top:14px" }, el("h2", {}, "Audio"),
      el("div", { class: "row" }, el("span", { class: "muted" }, "From"), start, el("span", { class: "muted" }, "to"), end), el("div", { style: "height:10px" }), usage,
      el("p", { class: "note" }, "Compressing converts lossless FLAC to Opus, about a fifth of the size or less, with speech still clear. Deleting removes the audio only: transcripts stay readable and searchable under History, and summaries are unaffected."),
      el("div", { class: "row" }, compress), el("div", { class: "row", style: "margin-top:8px" }, remove)),
    el("div", { class: "card" }, el("h2", {}, "Where your data lives"),
      el("p", {}, cfg.home), el("p", { class: "note" }, "One folder per day holding audio files, transcript.md, segments.jsonl and summary.md. All of it is readable without this app."),
      el("div", { class: "row" }, el("button", { class: "btn", onclick: (e) => act(e.target, () => api("/open-folder", { method: "POST", body: {} }).then(() => undefined)) }, "Open data folder")))];
}

// --- settings --------------------------------------------------------------------------------

const LLM_PRESETS = [["Ollama (local)", "http://localhost:11434/v1"], ["LM Studio (local)", "http://localhost:1234/v1"], ["OpenAI", "https://api.openai.com/v1"]];
const PROVIDER_LABELS = { none: "None (rules only)", anthropic: "Claude (Anthropic)", openai_compatible: "ChatGPT, or a local model (OpenAI-compatible)" };
const CHANNEL_LABELS = { desktop: "Desktop notification", email: "Email", ntfy: "Phone push (ntfy)" };

async function renderSettings() {
  const cfg = await api("/settings");
  const s = cfg.settings;

  // Every change is saved shortly after it is made; there is no Save button to forget.
  const fields = []; // [section, key, input, kind]
  const status = el("span", { class: "small muted", role: "status" }, "Changes are saved as you make them.");
  let timer = null, saving = Promise.resolve();
  const scheduleSave = () => { clearTimeout(timer); status.textContent = "Saving…"; status.className = "small muted"; timer = setTimeout(() => { timer = null; saving = save().catch(() => {}); }, 400); };
  const flush = async () => { if (timer) { clearTimeout(timer); timer = null; saving = save(); } await saving; };
  const bind = (section, key, input, kind = "text") => { fields.push([section, key, input, kind]); input.addEventListener("change", scheduleSave); return input; };
  const value = (section, key) => (section ? s[section][key] : s[key]);
  const text = (section, key, attrs = {}) => bind(section, key, el("input", { type: "text", value: value(section, key), ...attrs }));
  const number = (section, key, attrs = {}) => bind(section, key, el("input", { type: "number", value: value(section, key), style: "max-width:110px", ...attrs }), "number");
  const select = (section, key, options) => bind(section, key, el("select", {}, options.map(([v, label]) => el("option", { value: v, selected: v === value(section, key) }, label))));
  const check = (section, key, label) => el("label", { class: "check" }, bind(section, key, el("input", { type: "checkbox", checked: value(section, key) }), "bool"), label);
  const field = (label, control, note) => [el("label", { class: "field" }, el("span", {}, label), control), note && el("p", { class: "note" }, note)];
  const secret = (name, label) => {
    const input = el("input", { type: "password", placeholder: cfg.secrets[name] ? "Saved. Type a new one to replace it" : "Not set", autocomplete: "off" });
    const store = el("button", { class: "btn", onclick: (e) => act(e.target, async () => { await api("/secrets", { method: "POST", body: { name, value: input.value } }); input.value = ""; input.placeholder = "Saved. Type a new one to replace it"; return "Saved in the system credential store"; }) }, "Save key");
    return el("label", { class: "field" }, el("span", {}, label), el("div", { class: "row" }, input, store));
  };

  async function save() {
    const data = { recording: {}, speech: {}, llm: {}, summary: {}, reminders: {}, notify: {} };
    for (const [section, key, input, kind] of fields) {
      const v = kind === "bool" ? input.checked : kind === "number" ? Number(input.value) : input.value;
      if (section) data[section][key] = v; else data[key] = v;
    }
    data.reminders.channels = channelBoxes.filter((b) => b.checked).map((b) => b.dataset.channel);
    try {
      const r = await api("/settings", { method: "PUT", body: data });
      // Show any value the app had to bring into range.
      for (const [section, key, input, kind] of fields) {
        const v = section ? r.settings[section][key] : r.settings[key];
        if (kind === "number" && Number(input.value) !== v && document.activeElement !== input) input.value = v;
        if (key === "auto_time" && input.value !== v) input.value = v;
      }
      status.textContent = "All changes saved"; status.className = "small ok-text";
      hotkeyError.textContent = r.hotkey.error;
      pollStatus();
    } catch (e) {
      status.textContent = "Not saved: " + e.message; status.className = "small error-text";
      throw e;
    }
  }

  // Language model: each provider remembers its own model name, so switching never sends a local model's name to Claude.
  const provider = select("llm", "provider", cfg.providers.map((p) => [p, PROVIDER_LABELS[p] || p]));
  const model = bind("llm", "model", el("input", { type: "text", value: s.llm.model }));
  const baseUrl = bind("llm", "base_url", el("input", { type: "text", value: s.llm.base_url }));
  const modelFor = { [s.llm.provider]: s.llm.model };
  let current = provider.value;
  const privacy = el("p", { class: "note" });
  const updatePrivacy = () => {
    privacy.textContent = provider.value === "anthropic" || !/^https?:\/\/(localhost|127\.0\.0\.1)(:|\/|$)/.test(baseUrl.value)
      ? "Privacy: with this choice, transcript text is sent to that company's servers each time a summary is made (and for reminders, if that option is on below). Audio is never sent."
      : "Privacy: a model server on this computer keeps all text local.";
  };
  baseUrl.addEventListener("input", updatePrivacy);
  const llmExtra = el("div", {});
  const showLlm = () => {
    const p = provider.value;
    model.placeholder = p === "anthropic" ? "Leave empty for claude-opus-5-5" : "For example llama3.2 (the name shown by “ollama list”)";
    updatePrivacy();
    llmExtra.replaceChildren(...(p === "none"
      ? [el("p", { class: "note" }, "Without a language model, notes and reminders are still found from spoken phrases such as “note to self”, but the daily summary cannot sort your day into tasks.")]
      : [field("Model", model),
        p === "openai_compatible" && field("Server address", el("div", {}, baseUrl, el("div", { class: "row small", style: "margin-top:4px" },
          LLM_PRESETS.map(([label, url]) => el("button", { class: "btn link", onclick: () => { baseUrl.value = url; updatePrivacy(); scheduleSave(); } }, label))))),
        p === "anthropic" && secret("anthropic_api_key", "Anthropic API key"),
        p === "openai_compatible" && secret("openai_api_key", "API key (not needed for local servers)"),
        privacy,
        el("div", { class: "row" }, el("button", { class: "btn", onclick: (e) => act(e.target, async () => { await flush(); const r = await api("/llm/test", { method: "POST" }); return `Connected. The model replied: “${r.reply}”`; }) }, "Test connection"))]
    ).flat(Infinity).filter(Boolean));
  };
  provider.addEventListener("change", () => { modelFor[current] = model.value; current = provider.value; model.value = modelFor[current] ?? ""; showLlm(); });

  const hotkeyError = el("div", { class: "error-text small", style: "margin-top:4px" });
  const channelBoxes = [];
  const channels = cfg.channels.map((c) => {
    const box = el("input", { type: "checkbox", checked: s.reminders.channels.includes(c), "data-channel": c, onchange: scheduleSave });
    channelBoxes.push(box);
    return el("div", { class: "row", style: "margin-bottom:6px" }, el("label", { class: "check", style: "min-width:200px" }, box, CHANNEL_LABELS[c] || c),
      el("button", { class: "btn", onclick: (e) => act(e.target, async () => { await flush(); await api("/notify/test", { method: "POST", body: { channel: c } }); return "Test sent"; }) }, "Send test"));
  });

  const devices = [["", "System default"], ...cfg.input_devices.map((d) => [d, d])];
  if (s.recording.input_device && !cfg.input_devices.includes(s.recording.input_device)) devices.push([s.recording.input_device, `${s.recording.input_device} (not connected; the system default is used)`]);

  const autostart = cfg.autostart.supported && el("label", { class: "check" },
    el("input", { type: "checkbox", checked: cfg.autostart.enabled, onchange: async (e) => {
      try { const r = await api("/autostart", { method: "POST", body: { on: e.target.checked } }); e.target.checked = r.enabled; }
      catch (err) { e.target.checked = !e.target.checked; message("Could not change this", err.message); }
    } }), "Start Uncle Virgil when I sign in to Windows (opens minimized; continuous recording resumes if it was on)");

  const m = state.status?.models;
  showLlm();
  hotkeyError.textContent = cfg.hotkey.error;
  return [el("div", { class: "row spread sticky-head" }, el("h1", {}, "Settings"), status),
    el("div", { class: "card", style: "margin-top:14px" }, el("h2", {}, "You"),
      field("Your name", text(null, "user_name"), "Used in summaries until your voice is recorded under People.")),
    el("div", { class: "card" }, el("h2", {}, "Recording"),
      field("Microphone", select("recording", "input_device", devices)),
      field("Pause that ends a recording (seconds)", number("recording", "block_silence_s", { min: 1, max: 120, step: 0.5 }), "In continuous mode, a recording is closed and transcribed after this much silence."),
      field("Longest single recording (seconds)", number("recording", "max_block_s", { min: 30, max: 3600, step: 10 })),
      cfg.hotkey.supported && field("Dictation shortcut", el("div", {},
        bind("recording", "hotkey", el("input", { type: "text", value: s.recording.hotkey, placeholder: "For example Ctrl+Alt+D", style: "max-width:200px" })),
        hotkeyError),
        "Works from any program: press it to start dictating and again to stop, with a short sound each time. Leave empty to turn it off."),
      autostart),
    el("div", { class: "card" }, el("h2", {}, "Speech models"),
      field("Transcription model", select("speech", "asr_model", cfg.asr_models.map((x) => [x.key, `${x.label}, ${x.size_mb} MB${x.installed ? " (installed)" : ""}`])),
        "All transcription and speaker recognition runs on this computer. Models are downloaded once from GitHub. English only for now."),
      m && !m.ready && el("div", { class: "row" }, el("span", { class: "note" }, "Missing: " + m.missing.map((x) => x.label).join(", ") + "."),
        el("button", { class: "btn", onclick: (e) => act(e.target, async () => { await flush(); await api("/models/download", { method: "POST" }); return "Downloading; progress shows at the top of the window"; }) }, "Download now"))),
    el("div", { class: "card", id: "settings-llm" }, el("h2", {}, "Language model"), field("Provider", provider), llmExtra),
    el("div", { class: "card" }, el("h2", {}, "Daily summary"),
      field("Summarize each day at", bind("summary", "auto_time", el("input", { type: "time", value: s.summary.auto_time, style: "max-width:130px" })), "Leave empty to summarize only when you ask."),
      field("New conversation after a pause of (seconds)", number("summary", "conversation_gap_s", { min: 10, max: 3600, step: 10 })),
      field("Quiet minutes counted toward a task", number("summary", "gap_attribution_min", { min: 0, max: 240 }), "After a conversation ends, up to this many minutes of silence are counted as time spent on the same task. Set 0 to count speaking time only.")),
    el("div", { class: "card" }, el("h2", {}, "Reminders"),
      field("Notify this many minutes ahead", number("reminders", "lead_minutes", { min: 0, max: 1440 })),
      el("p", { class: "muted", style: "margin:12px 0 6px" }, "Notify me by"), channels,
      el("div", { style: "margin:10px 0" }, check("reminders", "confirm_new", "When a reminder is heard, show a desktop notification with the time it was understood as, so a misheard time can be fixed")),
      el("div", { style: "margin:10px 0" }, check("reminders", "use_llm", "Also use the language model to find reminders (sends your own words from each recording to the model)")),
      el("h3", {}, "Email"), field("Send to", text("notify", "email_to")), field("Send from", text("notify", "email_from")),
      field("SMTP server", text("notify", "smtp_host", { placeholder: "smtp.example.com" })), field("SMTP port", number("notify", "smtp_port")),
      field("SMTP user", text("notify", "smtp_user")), secret("smtp_password", "SMTP password"),
      el("h3", {}, "Phone push (ntfy)"), field("Server", text("notify", "ntfy_server")), field("Topic", text("notify", "ntfy_topic"),
        "Install the ntfy app on your phone and subscribe to the same topic. The reminder text passes through the ntfy server; use a long, hard-to-guess topic, or host your own server."),
      secret("ntfy_token", "Access token (optional)")),
    cfg.plugins.length ? el("p", { class: "note" }, "Plugins loaded: " + cfg.plugins.join(", ")) : null];
}

const PAGES = { history: renderHistory, reminders: renderReminders, people: renderPeople, storage: renderStorage, settings: renderSettings };

// --- start -----------------------------------------------------------------------------------

go("today");
pollStatus();
setInterval(pollStatus, 1000);
