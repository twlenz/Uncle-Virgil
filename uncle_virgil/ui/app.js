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
const clock = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const clockSec = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const longDate = (d) => new Date(d + "T12:00").toLocaleDateString([], { weekday: "long", month: "long", day: "numeric", year: "numeric" });
const isoDate = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const megabytes = (b) => (b >= 1e9 ? (b / 1e9).toFixed(2) + " GB" : (b / 1e6).toFixed(1) + " MB");
function duration(seconds) {
  const m = Math.round(seconds / 60);
  if (m < 1) return "under 1m";
  return m >= 60 ? `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m` : `${m}m`;
}

/** Run an action from a button: disable it meanwhile, show any error next to it. */
async function act(button, fn) {
  const msg = button.parentElement.querySelector(".act-msg") || button.parentElement.appendChild(el("span", { class: "act-msg small" }));
  msg.textContent = ""; msg.className = "act-msg small";
  button.disabled = true;
  try {
    const text = await fn();
    if (typeof text === "string") { msg.textContent = text; msg.classList.add("ok-text"); }
  } catch (e) {
    msg.textContent = e.message; msg.classList.add("error-text");
  } finally { button.disabled = false; }
}

function showDialog(...content) {
  const d = $("dialog");
  d.replaceChildren(...content);
  d.showModal();
  return d;
}

/** Ask for confirmation of something destructive. Resolves true or false. */
function confirmDialog(title, text, actionLabel) {
  return new Promise((resolve) => {
    const d = showDialog(
      el("h2", {}, title), el("p", {}, text),
      el("div", { class: "row", style: "justify-content:flex-end" },
        el("button", { class: "btn", onclick: () => { d.close(); resolve(false); } }, "Cancel"),
        el("button", { class: "btn danger", onclick: () => { d.close(); resolve(true); } }, actionLabel)));
  });
}

// --- state and navigation ------------------------------------------------------------------

const state = { view: "today", date: null, tab: null, status: null, lastPending: 0 };
const NAV = [["today", "Today"], ["yesterday", "Yesterday"], ["history", "History"], ["reminders", "Reminders"], ["people", "People"], ["storage", "Storage"], ["settings", "Settings"]];

function go(view, date) {
  state.view = view; state.date = date || null; state.tab = null;
  $("nav").replaceChildren(...NAV.map(([key, label]) => el("button", { class: key === view ? "active" : "", onclick: () => go(key) }, label)));
  render();
}

function viewedDate() {
  const today = new Date();
  if (state.view === "today") return isoDate(today);
  if (state.view === "yesterday") { today.setDate(today.getDate() - 1); return isoDate(today); }
  return state.date;
}

async function render() {
  const view = $("view");
  try {
    if (["today", "yesterday", "day"].includes(state.view)) await renderDay(view, viewedDate());
    else await ({ history: renderHistory, reminders: renderReminders, people: renderPeople, storage: renderStorage, settings: renderSettings })[state.view](view);
  } catch (e) {
    view.replaceChildren(el("p", { class: "error-text" }, e.message));
  }
}

// --- recording bar and status ----------------------------------------------------------------

async function pollStatus() {
  let s;
  try { s = await api("/status"); } catch { $("recstate").textContent = "Not connected"; return; }
  state.status = s;
  const dictating = s.mode === "dictation";
  $("dictate").textContent = dictating ? "Stop dictation" : "Dictate";
  $("dictate").classList.toggle("recording", dictating);
  $("dictate").classList.toggle("primary", !dictating);
  $("continuous").checked = s.continuous;
  $("level").style.width = Math.round(s.level * 100) + "%";
  $("recstate").textContent = s.recorder_error || { dictation: "Recording…", sample: "Recording voice sample…", continuous: s.speaking ? "Hearing speech" : "Listening", idle: "" }[s.mode];
  $("recstate").classList.toggle("error-text", !!s.recorder_error);
  $("foot").textContent = s.pending ? `Transcribing ${s.pending} recording${s.pending > 1 ? "s" : ""}…` : "";
  renderBanners(s);
  // Refresh the open day when background transcription finishes something.
  if (s.pending < state.lastPending && viewedDate() === s.today && !$("dialog").open) render();
  state.lastPending = s.pending;
}

function renderBanners(s) {
  const out = [];
  const m = s.models;
  if (m.progress.active) {
    out.push(el("div", { class: "banner" }, el("span", { class: "grow" }, `Downloading ${m.progress.model}… ${Math.round(m.progress.done_mb)} of ${Math.round(m.progress.total_mb)} MB`),
      el("progress", { max: m.progress.total_mb || 1, value: m.progress.done_mb })));
  } else if (!m.ready) {
    const size = m.missing.reduce((a, x) => a + x.size_mb, 0);
    out.push(el("div", { class: "banner alert" },
      el("span", { class: "grow" }, m.progress.error ? `Model download failed: ${m.progress.error}` : `Speech models are not installed yet (one-time download, about ${size} MB). Recordings are kept and transcribed once they are.`),
      el("button", { class: "btn primary", onclick: (e) => act(e.target, () => api("/models/download", { method: "POST" })) }, "Download models")));
  }
  if (s.pipeline_error) out.push(el("div", { class: "banner error" }, el("span", { class: "grow" }, "A recording could not be transcribed: " + s.pipeline_error)));
  for (const r of s.alerts) {
    out.push(el("div", { class: "banner alert" }, el("span", { class: "grow" }, el("strong", {}, "Reminder: "), `${r.text} (${clock(r.due)})`),
      el("button", { class: "btn", onclick: async () => { await api(`/reminders/${r.id}`, { method: "PATCH", body: { status: "done" } }); pollStatus(); } }, "Done")));
  }
  // Rebuild only when something changed, so buttons are not replaced mid-click.
  const key = JSON.stringify([m.progress, m.ready, s.pipeline_error, s.alerts.map((r) => r.id)]);
  if (key !== renderBanners.key) { renderBanners.key = key; $("banners").replaceChildren(...out); }
  else if (m.progress.active) $("banners").querySelector("progress").value = m.progress.done_mb;
}

$("dictate").addEventListener("click", async () => {
  try { await api("/record/dictation", { method: "POST", body: { on: state.status?.mode !== "dictation" } }); }
  catch (e) { $("recstate").textContent = e.message; }
  pollStatus();
});
$("continuous").addEventListener("change", async (e) => {
  try { await api("/record/continuous", { method: "POST", body: { on: e.target.checked } }); }
  catch (err) { e.target.checked = false; showDialog(el("h2", {}, "Could not start"), el("p", {}, err.message), el("button", { class: "btn", onclick: () => $("dialog").close() }, "Close")); }
  pollStatus();
});

// --- day view --------------------------------------------------------------------------------

async function renderDay(view, date) {
  const day = await api(`/days/${date}`);
  const hasText = day.conversations.length > 0;
  const tab = state.tab || (day.summary ? "summary" : "transcript");
  const fileInput = el("input", { type: "file", accept: "audio/*,.flac,.opus,.m4a", style: "display:none", onchange: (e) => importFile(e.target, date) });
  const head = el("div", { class: "row spread" },
    el("div", {}, el("h1", {}, state.view === "day" ? longDate(date) : state.view === "today" ? "Today" : "Yesterday"),
      state.view !== "day" && el("div", { class: "muted" }, longDate(date))),
    el("div", { class: "row" }, fileInput,
      el("button", { class: "btn", title: "Add an existing recording to this day", onclick: () => fileInput.click() }, "Import audio…"),
      el("button", { class: "btn", onclick: (e) => act(e.target, () => api("/open-folder", { method: "POST", body: { date } }).then(() => undefined)) }, "Open folder")));
  const tabs = el("div", { class: "tabs" }, ["summary", "transcript"].map((t) =>
    el("button", { class: t === tab ? "active" : "", onclick: () => { state.tab = t; render(); } }, t === "summary" ? "Summary" : "Transcript")));
  const body = !hasText
    ? el("p", { class: "muted" }, "Nothing has been transcribed for this day. Press Dictate, or switch on continuous recording.")
    : tab === "summary" ? summaryPanel(day) : transcriptPanel(day);
  view.replaceChildren(head, tabs, body);
  if (day.job?.state === "running") setTimeout(() => { if (viewedDate() === date) render(); }, 2000);
}

async function importFile(input, date) {
  if (!input.files[0]) return;
  const form = new FormData();
  form.append("file", input.files[0]);
  form.append("started", `${date}T${new Date().toTimeString().slice(0, 8)}`);
  try {
    const r = await api("/import", { method: "POST", form });
    showDialog(el("h2", {}, "Import started"), el("p", {}, `${duration(r.seconds)} of audio was split into ${r.blocks} recording${r.blocks === 1 ? "" : "s"} and is being transcribed.`),
      el("button", { class: "btn", onclick: () => $("dialog").close() }, "Close"));
  } catch (e) {
    showDialog(el("h2", {}, "Import failed"), el("p", {}, e.message), el("button", { class: "btn", onclick: () => $("dialog").close() }, "Close"));
  }
  pollStatus();
}

function summaryPanel(day) {
  const s = day.summary;
  const running = day.job?.state === "running";
  const generate = el("button", { class: "btn" + (s ? "" : " primary"), disabled: running, onclick: async () => { await api(`/days/${day.date}/summarize`, { method: "POST" }); render(); } },
    running ? "Summarizing…" : s ? "Regenerate" : "Generate summary");
  const noLlm = day.llm === "none" && el("p", { class: "note" }, "No language model is connected, so a summary lists notes and reminders but cannot identify tasks. Connect one in Settings.");
  const failed = day.job?.state === "error" && el("p", { class: "error-text" }, "The last summary attempt failed: " + day.job.error);
  if (!s) return el("div", { class: "card" }, el("p", {}, "No summary has been made for this day yet."), noLlm, failed, el("div", { class: "row" }, generate));

  const t = s.totals;
  const out = [el("div", { class: "card" },
    el("p", { class: "muted" }, `${t.conversations} conversation${t.conversations === 1 ? "" : "s"} between ${clock(t.first)} and ${clock(t.last)}, ${duration(t.speech_seconds)} of speech.`),
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
    s.notes.length ? el("ul", { class: "list" }, s.notes.map((n) => el("li", {}, el("span", { class: "muted" }, n.time), el("span", { class: "tag" }, n.kind), el("span", { class: "grow" }, n.text))))
      : el("p", { class: "muted" }, "None recorded.")));
  if (s.reminders.length) out.push(el("div", { class: "card" }, el("h2", {}, "Reminders set"),
    el("ul", { class: "list" }, s.reminders.map((r) => el("li", {}, el("span", { class: "grow" }, r.text), el("span", { class: "muted" }, new Date(r.due).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })))))));
  return el("div", {}, out);
}

function transcriptPanel(day) {
  const play = (seg) => {
    const p = $("player");
    p.src = `/api/audio/${day.date}/${seg.block}#t=${seg.offset.toFixed(1)}`;
    p.play();
  };
  return el("div", { class: "card" }, day.conversations.map((c) => [
    el("div", { class: "conv-head" }, `${clock(c.start)} to ${clock(c.end)}`),
    c.segments.map((seg) => {
      const unknown = /^unk-\d+$/.test(seg.speaker_id);
      return el("div", { class: "utt" }, el("time", {}, clockSec(seg.start)),
        el("span", { class: "who" + (unknown ? " unknown" : ""), title: unknown ? "Name this speaker" : seg.speaker, onclick: unknown ? () => nameSpeaker(day.date, seg) : null }, seg.speaker),
        el("span", { class: "text" }, seg.text),
        seg.has_audio ? el("button", { class: "play", title: "Play from here", onclick: () => play(seg) }, "▶") : el("span"));
    })]));
}

function nameSpeaker(date, seg) {
  const name = el("input", { type: "text", placeholder: "Name" });
  const me = el("input", { type: "checkbox" });
  const save = el("button", { class: "btn primary", onclick: (e) => act(e.target, async () => {
    await api(`/days/${date}/speakers/${seg.speaker_id}`, { method: "POST", body: { name: name.value, is_user: me.checked } });
    $("dialog").close(); render();
  }) }, "Save");
  showDialog(el("h2", {}, `Who is ${seg.speaker}?`),
    el("p", { class: "note" }, "Their voiceprint is saved on this computer so they are recognized by name from now on. You can remove it under People."),
    el("label", { class: "field" }, el("span", {}, "Name"), name), el("label", { class: "check" }, me, "This is me"),
    el("div", { class: "row", style: "justify-content:flex-end;margin-top:12px" }, el("button", { class: "btn", onclick: () => $("dialog").close() }, "Cancel"), save));
  name.focus();
}

// --- history ---------------------------------------------------------------------------------

async function renderHistory(view) {
  const days = await api("/days");
  const pick = el("input", { type: "date", max: isoDate(new Date()), onchange: (e) => e.target.value && go("day", e.target.value) });
  view.replaceChildren(el("h1", {}, "History"),
    el("div", { class: "card", style: "margin-top:14px" }, el("label", { class: "field" }, el("span", {}, "Go to a day"), pick),
      days.length ? el("ul", { class: "list" }, days.map((d) => el("li", {},
        el("button", { class: "btn link grow", style: "text-align:left", onclick: () => go("day", d.date) }, longDate(d.date)),
        d.has_summary && el("span", { class: "tag" }, "summary"))))
        : el("p", { class: "muted" }, "No days recorded yet.")));
}

// --- reminders -------------------------------------------------------------------------------

async function renderReminders(view) {
  const all = await api("/reminders");
  const text = el("input", { type: "text", placeholder: "What to be reminded of" });
  const due = el("input", { type: "datetime-local" });
  const add = el("button", { class: "btn primary", onclick: (e) => act(e.target, async () => { await api("/reminders", { method: "POST", body: { text: text.value, due: due.value } }); render(); }) }, "Add reminder");
  const when = (r) => new Date(r.due).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  const item = (r) => el("li", {}, el("span", { class: "muted", style: "width:170px" }, when(r)), el("span", { class: "grow" }, r.text),
    el("span", { class: "tag", title: { rule: "Heard in your speech", llm: "Found by the language model", manual: "Added by hand" }[r.source] }, r.source === "manual" ? "manual" : "heard"),
    r.status === "missed" && el("span", { class: "tag" }, "missed"),
    r.status !== "done" && el("button", { class: "btn", onclick: async () => { await api(`/reminders/${r.id}`, { method: "PATCH", body: { status: "done" } }); render(); pollStatus(); } }, "Done"),
    el("button", { class: "btn", onclick: async () => { await api(`/reminders/${r.id}`, { method: "DELETE" }); render(); pollStatus(); } }, "Delete"));
  const section = (title, items, empty) => el("div", { class: "card" }, el("h2", {}, title), items.length ? el("ul", { class: "list" }, items.map(item)) : el("p", { class: "muted" }, empty));
  view.replaceChildren(el("h1", {}, "Reminders"),
    el("p", { class: "note", style: "margin-top:8px" }, "Say “note to self” or “remind me” followed by what and when, for example “note to self, meeting with Jack at 5 pm”. You are notified ahead of time by the methods chosen in Settings."),
    el("div", { class: "card" }, el("div", { class: "row" }, text, due, add)),
    section("Upcoming", all.filter((r) => r.status === "pending"), "Nothing coming up."),
    section("Notified", all.filter((r) => r.status === "notified"), "Nothing waiting."),
    section("Past", all.filter((r) => ["done", "missed"].includes(r.status)).reverse().slice(0, 50), "Nothing yet."));
}

// --- people ----------------------------------------------------------------------------------

async function renderPeople(view) {
  const [speakers, cfg] = await Promise.all([api("/speakers"), api("/settings")]);
  const hasOwner = speakers.some((s) => s.is_user);
  const name = el("input", { type: "text", value: hasOwner ? "" : cfg.settings.user_name === "Me" ? "" : cfg.settings.user_name, placeholder: "Name" });
  const me = el("input", { type: "checkbox", checked: !hasOwner });
  const start = el("button", { class: "btn primary", onclick: (e) => act(e.target, async () => {
    if (!name.value.trim()) throw new Error("Enter a name first");
    await api("/speakers/sample", { method: "POST", body: { on: true } });
    recordSample(name.value, me.checked);
  }) }, "Record a voice sample");
  view.replaceChildren(el("h1", {}, "People"),
    el("p", { class: "note", style: "margin-top:8px" }, "Voiceprints let transcripts show names. They are stored only on this computer (speakers.json in the data folder). Other voices appear as Speaker 1, 2… and can be named from a transcript by clicking the label."),
    el("div", { class: "card" }, el("h2", {}, hasOwner ? "Add a person" : "Add your voice"),
      el("label", { class: "field" }, el("span", {}, "Name"), name), el("label", { class: "check" }, me, "This is me"),
      el("p", { class: "note" }, "You will read a short passage aloud for about 20 seconds. Adding another sample for the same name improves recognition."),
      el("div", { class: "row" }, start)),
    el("div", { class: "card" }, el("h2", {}, "Known voices"),
      speakers.length ? el("ul", { class: "list" }, speakers.map((s) => {
        const input = el("input", { type: "text", value: s.name, style: "max-width:220px" });
        return el("li", {}, input, s.is_user && el("span", { class: "tag" }, "you"), el("span", { class: "muted grow" }, `${s.samples} sample${s.samples > 1 ? "s" : ""}`),
          el("button", { class: "btn", onclick: (e) => act(e.target, async () => { await api(`/speakers/${s.id}`, { method: "PATCH", body: { name: input.value } }); return "Renamed"; }) }, "Rename"),
          el("button", { class: "btn danger", onclick: async () => {
            if (await confirmDialog("Forget this voice?", `${s.name}'s voiceprint will be deleted. Existing transcripts keep the name.`, "Forget")) { await api(`/speakers/${s.id}`, { method: "DELETE" }); render(); }
          } }, "Forget"));
      })) : el("p", { class: "muted" }, "Nobody yet.")));
}

function recordSample(name, isUser) {
  const started = Date.now();
  const timer = el("strong", {}, "0s");
  const tick = setInterval(() => { timer.textContent = Math.round((Date.now() - started) / 1000) + "s"; }, 500);
  const finish = async (save, button) => {
    clearInterval(tick);
    const body = { on: false, name: save ? name : "", is_user: isUser };
    if (!save) { await api("/speakers/sample", { method: "POST", body }).catch(() => {}); $("dialog").close(); return; }
    await act(button, async () => { await api("/speakers/sample", { method: "POST", body }); $("dialog").close(); render(); });
  };
  showDialog(el("h2", {}, "Recording… ", timer), el("p", {}, "Read this aloud in your normal voice:"),
    el("div", { class: "read-aloud" }, "Uncle Virgil keeps a private record of my day. It listens for the things I say, writes them down with the time, and reminds me of what I ask it to remember. Everything it hears stays on my own computer unless I decide otherwise. When I say “note to self”, it saves what follows as a note."),
    el("div", { class: "row", style: "justify-content:flex-end" }, el("button", { class: "btn", onclick: () => finish(false) }, "Cancel"),
      el("button", { class: "btn primary", onclick: (e) => finish(true, e.target) }, "Stop and save")));
  $("dialog").addEventListener("cancel", () => finish(false), { once: true });
}

// --- storage ---------------------------------------------------------------------------------

async function renderStorage(view) {
  const [days, cfg] = await Promise.all([api("/days"), api("/settings")]);
  const today = isoDate(new Date());
  const start = el("input", { type: "date", value: days.length ? days[days.length - 1].date : today, max: today });
  const end = el("input", { type: "date", value: today, max: today });
  const usage = el("p", {});
  const range = () => ({ start: start.value, end: end.value });
  const refresh = async () => {
    try {
      const u = await api(`/storage/usage?start=${start.value}&end=${end.value}`);
      usage.textContent = u.files ? `${u.files} audio file${u.files === 1 ? "" : "s"} in this range: ${megabytes(u.bytes)}, ${duration(u.seconds)} of recordings. ${u.compressed_files} already compressed.` : "No audio in this range.";
    } catch (e) { usage.textContent = e.message; }
  };
  start.onchange = end.onchange = refresh;
  const compress = el("button", { class: "btn", onclick: (e) => act(e.target, async () => {
    await api("/storage/compress", { method: "POST", body: range() });
    for (;;) {
      await new Promise((r) => setTimeout(r, 700));
      const job = await api("/jobs/compress");
      if (job.state === "error") throw new Error(job.error);
      if (job.state === "done") { refresh(); return job.result.files ? `Compressed ${job.result.files} files: ${megabytes(job.result.bytes_before)} down to ${megabytes(job.result.bytes_after)}.` : "Nothing left to compress in this range."; }
    }
  }) }, "Compress audio in range");
  const remove = el("button", { class: "btn danger", onclick: async (e) => {
    if (!(await confirmDialog("Delete audio?", `All audio recorded from ${start.value} to ${end.value} will be permanently deleted. Transcripts and summaries are kept.`, "Delete audio"))) return;
    act(e.target, async () => { const r = await api("/storage/delete", { method: "POST", body: range() }); refresh(); return `Deleted ${r.files} files, freed ${megabytes(r.bytes)}.`; });
  } }, "Delete audio in range, keep text");
  view.replaceChildren(el("h1", {}, "Storage"),
    el("div", { class: "card", style: "margin-top:14px" }, el("h2", {}, "Audio"),
      el("div", { class: "row" }, el("span", { class: "muted" }, "From"), start, el("span", { class: "muted" }, "to"), end), el("div", { style: "height:10px" }), usage,
      el("p", { class: "note" }, "Compressing converts lossless FLAC to Opus, about a fifth of the size or less, with speech still clear. Deleting removes the audio only; the text stays searchable and summaries are unaffected."),
      el("div", { class: "row" }, compress), el("div", { class: "row", style: "margin-top:8px" }, remove)),
    el("div", { class: "card" }, el("h2", {}, "Where your data lives"),
      el("p", {}, cfg.home), el("p", { class: "note" }, "One folder per day holding audio files, transcript.md, segments.jsonl and summary.md. All of it is readable without this app."),
      el("div", { class: "row" }, el("button", { class: "btn", onclick: (e) => act(e.target, () => api("/open-folder", { method: "POST", body: {} }).then(() => undefined)) }, "Open data folder"))));
  refresh();
}

// --- settings --------------------------------------------------------------------------------

const LLM_PRESETS = [["Ollama (local)", "http://localhost:11434/v1"], ["LM Studio (local)", "http://localhost:1234/v1"], ["OpenAI", "https://api.openai.com/v1"]];
const PROVIDER_LABELS = { none: "None (rules only)", anthropic: "Claude (Anthropic)", openai_compatible: "ChatGPT, or a local model (OpenAI-compatible)" };
const CHANNEL_LABELS = { desktop: "Desktop notification", email: "Email", ntfy: "Phone push (ntfy)" };

async function renderSettings(view) {
  const cfg = await api("/settings");
  const s = cfg.settings;
  const fields = [];  // [section, key, input, kind]
  const bind = (section, key, input, kind = "text") => { fields.push([section, key, input, kind]); return input; };
  const text = (section, key, attrs = {}) => bind(section, key, el("input", { type: "text", value: section ? s[section][key] : s[key], ...attrs }));
  const number = (section, key, attrs = {}) => bind(section, key, el("input", { type: "number", value: s[section][key], style: "max-width:110px", ...attrs }), "number");
  const select = (section, key, options) => bind(section, key, el("select", {}, options.map(([value, label]) => el("option", { value, selected: value === s[section][key] }, label))));
  const check = (section, key, label) => el("label", { class: "check" }, bind(section, key, el("input", { type: "checkbox", checked: s[section][key] }), "bool"), label);
  const field = (label, control, note) => [el("label", { class: "field" }, el("span", {}, label), control), note && el("p", { class: "note" }, note)];
  const secret = (name, label) => {
    const input = el("input", { type: "password", placeholder: cfg.secrets[name] ? "Saved. Type to replace" : "Not set", autocomplete: "off" });
    const save = el("button", { class: "btn", onclick: (e) => act(e.target, async () => { await api("/secrets", { method: "POST", body: { name, value: input.value } }); input.value = ""; return "Saved in the system credential store"; }) }, "Save key");
    return el("label", { class: "field" }, el("span", {}, label), el("div", { class: "row" }, input, save));
  };

  const provider = select("llm", "provider", cfg.providers.map((p) => [p, PROVIDER_LABELS[p] || p]));
  const baseUrl = text("llm", "base_url");
  const llmExtra = el("div", {});
  const showLlm = () => {
    const p = provider.value;
    llmExtra.replaceChildren(el("div", {}, p === "none" ? [el("p", { class: "note" }, "Without a language model, notes and reminders are still found by spoken trigger phrases, but daily summaries cannot identify tasks.")] : [
      field("Model", text("llm", "model", { placeholder: p === "anthropic" ? "claude-opus-5-5" : "for example llama3.2 or gpt-5" })),
      p === "openai_compatible" && field("Server address", el("div", {}, baseUrl, el("div", { class: "row small", style: "margin-top:4px" }, LLM_PRESETS.map(([label, url]) => el("button", { class: "btn link", onclick: () => { baseUrl.value = url; } }, label))))),
      p === "anthropic" && secret("anthropic_api_key", "Anthropic API key"),
      p === "openai_compatible" && secret("openai_api_key", "API key (not needed for local servers)"),
      el("p", { class: "note" }, p === "anthropic" || !/localhost|127\.0\.0\.1/.test(baseUrl.value)
        ? "Privacy: with this choice, transcript text is sent to that company's servers each time a summary is made (and for reminders, if enabled below). Audio is never sent."
        : "Privacy: a model server on this computer keeps all text local."),
      el("div", { class: "row" }, el("button", { class: "btn", onclick: (e) => act(e.target, async () => { await save(); const r = await api("/llm/test", { method: "POST" }); return `Connected. The model replied: “${r.reply}”`; }) }, "Save and test connection")),
    ]));
  };
  provider.onchange = showLlm;

  const channels = cfg.channels.map((c) => {
    const box = el("input", { type: "checkbox", checked: s.reminders.channels.includes(c), "data-channel": c });
    return el("div", { class: "row", style: "margin-bottom:6px" }, el("label", { class: "check", style: "min-width:200px" }, box, CHANNEL_LABELS[c] || c),
      el("button", { class: "btn", onclick: (e) => act(e.target, async () => { await save(); await api("/notify/test", { method: "POST", body: { channel: c } }); return "Test sent"; }) }, "Save and send test"));
  });

  async function save() {
    const data = { recording: {}, speech: {}, llm: {}, summary: {}, reminders: {}, notify: {} };
    for (const [section, key, input, kind] of fields) {
      if (!input.isConnected) continue;
      const value = kind === "bool" ? input.checked : kind === "number" ? Number(input.value) : input.value;
      if (section) data[section][key] = value; else data[key] = value;
    }
    data.reminders.channels = [...view.querySelectorAll("[data-channel]")].filter((b) => b.checked).map((b) => b.dataset.channel);
    await api("/settings", { method: "PUT", body: data });
  }

  const m = state.status?.models;
  view.replaceChildren(el("h1", {}, "Settings"),
    el("div", { class: "card", style: "margin-top:14px" }, el("h2", {}, "You"),
      field("Your name", text(null, "user_name"), "Used in summaries until your voice is enrolled under People.")),
    el("div", { class: "card" }, el("h2", {}, "Speech models"),
      field("Transcription model", select("speech", "asr_model", cfg.asr_models.map((x) => [x.key, `${x.label}, ${x.size_mb} MB${x.installed ? " (installed)" : ""}`])),
        "All transcription and speaker recognition runs on this computer. Models are downloaded once from GitHub. English only for now."),
      m && !m.ready && el("p", { class: "note" }, "Missing: " + m.missing.map((x) => x.label).join(", ") + ". Save, then use Download models at the top of the window.")),
    el("div", { class: "card" }, el("h2", {}, "Recording"),
      field("Microphone", select("recording", "input_device", [["", "System default"], ...cfg.input_devices.map((d) => [d, d])])),
      field("Pause that ends a recording (seconds)", number("recording", "block_silence_s", { min: 1, step: 0.5 }), "In continuous mode, a recording is closed and transcribed after this much silence."),
      field("Longest single recording (seconds)", number("recording", "max_block_s", { min: 30, step: 10 }))),
    el("div", { class: "card" }, el("h2", {}, "Language model"), field("Provider", provider), llmExtra),
    el("div", { class: "card" }, el("h2", {}, "Daily summary"),
      field("Summarize each day at", bind("summary", "auto_time", el("input", { type: "time", value: s.summary.auto_time, style: "max-width:130px" })), "Leave empty to summarize only when you ask."),
      field("New conversation after a pause of (seconds)", number("summary", "conversation_gap_s", { min: 10, step: 10 })),
      field("Quiet minutes counted toward a task", number("summary", "gap_attribution_min", { min: 0 }), "After a conversation ends, up to this many minutes of silence are counted as time spent on the same task. Set 0 to count speaking time only.")),
    el("div", { class: "card" }, el("h2", {}, "Reminders"),
      field("Notify this many minutes ahead", number("reminders", "lead_minutes", { min: 0 })),
      el("p", { class: "muted", style: "margin:12px 0 6px" }, "Notify me by"), channels,
      el("div", { style: "margin:10px 0" }, check("reminders", "use_llm", "Also use the language model to find reminders (sends your own words from each recording to the model)")),
      el("h3", {}, "Email"), field("Send to", text("notify", "email_to")), field("Send from", text("notify", "email_from")),
      field("SMTP server", text("notify", "smtp_host", { placeholder: "smtp.example.com" })), field("SMTP port", number("notify", "smtp_port")),
      field("SMTP user", text("notify", "smtp_user")), secret("smtp_password", "SMTP password"),
      el("h3", {}, "Phone push (ntfy)"), field("Server", text("notify", "ntfy_server")), field("Topic", text("notify", "ntfy_topic"),
        "Install the ntfy app on your phone and subscribe to the same topic. The reminder text passes through the ntfy server; use a long, hard-to-guess topic, or host your own server."),
      secret("ntfy_token", "Access token (optional)")),
    el("div", { class: "row", style: "margin-bottom:30px" }, el("button", { class: "btn primary", onclick: (e) => act(e.target, async () => { await save(); pollStatus(); return "Saved"; }) }, "Save settings")),
    cfg.plugins.length ? el("p", { class: "note" }, "Plugins loaded: " + cfg.plugins.join(", ")) : null);
  showLlm();
}

// --- start -----------------------------------------------------------------------------------

go("today");
pollStatus();
setInterval(pollStatus, 1000);
