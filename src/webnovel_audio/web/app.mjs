// Webnovel Audio player: one page, no build step, no dependencies.
//
// The server knows who is listening only by a name picked here (no login); it
// keeps each person's place per series. The rules for "where am I / what's new"
// live in state.mjs so they can be tested without a browser.
import {
  advanceFurthest, chapterState, cssUrl, formatTime, furthestPoint, indexOfChapter, newCount,
  nextChapter, prevChapter, resumePoint,
} from "./state.mjs";

const $ = (sel) => document.querySelector(sel);
const KEY = { profile: "wna.profile", guest: "wna.guest", rate: "wna.rate" };
const RATES = [1, 1.15, 1.3, 1.5, 1.75, 2, 0.85];
const SLEEPS = [0, 15, 30, 45, 60];
const DEFAULT_ACCENT = "#7c8cff";

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* private mode */ } },
};

const S = {
  series: [], by: new Map(),
  profile: store.get(KEY.profile),
  pos: {},                                   // slug -> {chapter, t, furthest, furthest_t, updated}
  now: null,                                 // {slug, id} of the chapter loaded in the player
  listened: 0, lastT: 0, dirty: false,       // seconds actually listened to this chapter
  sleepAt: 0, sleepIdx: 0, loaded: false, podcastOpen: false,
  accents: new Map(),
};
const audio = $("#audio");

// ---------------------------------------------------------------- dom helpers
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid);
  return el;
}

const ICONS = {
  play: "M8 5v14l11-7z", pause: "M6 5h4v14H6zM14 5h4v14h-4z",
  prev: "M6 6h2v12H6zM9.5 12l8.5 6V6z", next: "M16 6h2v12h-2zM6 18l8.5-6L6 6z",
  back: "M15 5l-7 7 7 7",
};
function icon(name) {                         // static path data only, never page content
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
  p.setAttribute("d", ICONS[name]);
  if (name === "back") { p.setAttribute("fill", "none"); p.setAttribute("stroke", "currentColor"); p.setAttribute("stroke-width", "2.5"); p.setAttribute("stroke-linecap", "round"); p.setAttribute("stroke-linejoin", "round"); svg.style.width = "18px"; svg.style.height = "18px"; }
  svg.append(p);
  return svg;
}

function avatar(name) {
  let hue = 0;
  for (const ch of name || "?") hue = (hue * 31 + ch.charCodeAt(0)) % 360;
  const el = h("span", { class: "avatar" }, (name || "?")[0].toUpperCase());
  el.style.background = name ? `hsl(${hue} 70% 68%)` : "#8893a8";
  return el;
}

let toastTimer = 0;
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
}

// ------------------------------------------------------------------------ api
async function api(path, method = "GET", body) {
  const init = { method, cache: "no-store", headers: {} };
  if (body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
  const r = await fetch(path, init);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(data.error || r.statusText), { status: r.status });
  return data;
}

async function loadPositions() {
  if (!S.profile) { S.pos = {}; return; }
  const playing = S.now && S.pos[S.now.slug];           // ours is newer than the server's
  try {
    S.pos = (await api(`/api/positions/${encodeURIComponent(S.profile)}`)).series;
    if (playing) S.pos[S.now.slug] = playing;
  } catch (e) {
    if (e.status === 404) { setProfile(null); toast("That name no longer exists here"); }
  }
}

async function refresh() {
  try {
    const lib = await api("/api/library");
    S.series = lib.series;
    S.by = new Map(S.series.map((s) => [s.slug, s]));
    await loadPositions();
    S.loaded = true;
    render();
  } catch {
    if (!S.loaded) $("#main").replaceChildren(h("p", { class: "lead" }, "Can't reach the server. Is it running, and are you on the right network?"));
  }
}

function savePosition(keepalive = false) {
  if (!S.profile || !S.now || !S.dirty) return Promise.resolve();
  const pos = S.pos[S.now.slug];
  if (!pos) return Promise.resolve();
  S.dirty = false;
  return fetch(`/api/positions/${encodeURIComponent(S.profile)}/${encodeURIComponent(S.now.slug)}`, {
    method: "PUT", keepalive, headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chapter: pos.chapter, t: Math.round(pos.t * 10) / 10, furthest: pos.furthest }),
  }).then((r) => { if (!r.ok) S.dirty = true; }).catch(() => { S.dirty = true; });
}

// -------------------------------------------------------------- cover accents
async function accentFor(series) {
  if (!series.cover) return DEFAULT_ACCENT;
  if (S.accents.has(series.slug)) return S.accents.get(series.slug);
  let color = DEFAULT_ACCENT;
  try {                                      // same-origin covers can be sampled; remote ones are tainted
    const img = new Image();
    img.crossOrigin = "anonymous";
    img.src = series.cover;
    await img.decode();
    const c = document.createElement("canvas");
    c.width = c.height = 24;
    const g = c.getContext("2d", { willReadFrequently: true });
    g.drawImage(img, 0, 0, 24, 24);
    const px = g.getImageData(0, 0, 24, 24).data;
    let r = 0, gr = 0, b = 0, w = 0;
    for (let i = 0; i < px.length; i += 4) {
      const mx = Math.max(px[i], px[i + 1], px[i + 2]), mn = Math.min(px[i], px[i + 1], px[i + 2]);
      const wt = (mx - mn) * (mx - mn) + 1;  // favour the saturated, colourful pixels
      r += px[i] * wt; gr += px[i + 1] * wt; b += px[i + 2] * wt; w += wt;
    }
    color = brighten(r / w, gr / w, b / w);
  } catch { /* keep the default */ }
  S.accents.set(series.slug, color);
  return color;
}

function brighten(r, g, b) {                 // lift to a readable accent on a dark page
  r /= 255; g /= 255; b /= 255;
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
  let hue = 0;
  if (d) hue = mx === r ? ((g - b) / d) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
  hue = Math.round(((hue * 60) + 360) % 360);
  const sat = Math.min(100, Math.max(55, Math.round((d / (1 - Math.abs(mx + mn - 1) || 1)) * 100)));
  return `hsl(${hue} ${sat}% 66%)`;
}

async function applyAccent(series) {
  const color = series ? await accentFor(series) : DEFAULT_ACCENT;
  document.documentElement.style.setProperty("--accent", color);
}

// --------------------------------------------------------------------- routing
const route = () => {
  const m = location.hash.match(/^#\/s\/([^/?]+)/);
  return { slug: m ? decodeURIComponent(m[1]) : null };
};

function render() {
  renderTop();
  const { slug } = route();
  const series = slug && S.by.get(slug);
  const y = window.scrollY, sameView = $("#main").dataset.view === (series ? slug : "home");
  $("#main").replaceChildren(series ? seriesView(series) : homeView());
  $("#main").dataset.view = series ? slug : "home";
  if (sameView) window.scrollTo(0, y);
  else if (series) scrollToCurrent();
  else window.scrollTo(0, 0);
  document.body.classList.toggle("has-mini", !!S.now);
  applyAccent(series || (S.now && S.by.get(S.now.slug)) || null);
  renderMini();
}

function renderTop() {
  $("#who-btn").replaceChildren(avatar(S.profile), h("span", {}, S.profile || "Guest"));
}

// ------------------------------------------------------------------ home view
function recent(a, b) {
  const pa = S.pos[a.slug]?.updated || "", pb = S.pos[b.slug]?.updated || "";
  return pa === pb ? a.title.localeCompare(b.title) : pa < pb ? 1 : -1;
}

function coverEl(series) {
  const box = h("div", { class: "cover" });
  box.append(series.cover
    ? h("img", { src: series.cover, alt: "", loading: "lazy", onerror: (e) => e.target.replaceWith(h("div", { class: "ph" }, series.title[0])) })
    : h("div", { class: "ph" }, series.title[0]));
  return box;
}

function card(series) {
  const pos = S.pos[series.slug];
  const started = !!(S.profile && pos?.furthest);
  const n = newCount(series.chapters, pos?.furthest);
  const cover = coverEl(series);
  if (S.profile) {
    if (started) cover.append(h("span", { class: n ? "pill" : "pill quiet" }, n ? `${n} new` : "caught up"));
  }
  let meta = `${series.chapters.length} chapters`;
  if (started && pos.chapter) {
    const c = series.chapters.find((x) => x.id === pos.chapter);
    if (c) meta = `#${c.n} · ${formatTime(pos.t)}`;
  }
  if (series.paused) meta += " · paused";
  return h("a", { class: "card", href: `#/s/${encodeURIComponent(series.slug)}` },
    cover, h("div", { class: "t" }, series.title), h("div", { class: "m" }, meta));
}

function homeView() {
  if (!S.loaded) return h("p", { class: "lead" }, "Loading\u2026");
  const list = [...S.series].sort(recent);
  return h("div", {},
    h("h1", {}, S.profile ? `Hi, ${S.profile}` : "Library"),
    h("p", { class: "lead" }, S.profile
      ? "Pick up where you left off."
      : "Browsing as a guest. Pick who's listening (top right) to keep your place and see what's new."),
    list.length ? h("section", { class: "grid" }, list.map(card))
      : h("p", { class: "dim" }, "Nothing has been rendered yet."));
}

// ---------------------------------------------------------------- series view
function seriesView(series) {
  const pos = S.pos[series.slug];
  const resume = resumePoint(series.chapters, pos);
  const back = furthestPoint(series.chapters, pos);
  const n = newCount(series.chapters, pos?.furthest);
  const hero = h("section", { class: "hero" });
  hero.style.setProperty("--cover", series.cover ? cssUrl(series.cover) : "none");
  const facts = [`${series.chapters.length} chapters`, series.status ? series.status.toLowerCase() : null,
    series.paused ? "paused" : null].filter(Boolean).join(" · ");
  hero.append(coverEl(series), h("div", { class: "info" },
    h("h1", {}, series.title),
    series.author ? h("div", { class: "dim" }, series.author) : null,
    h("div", { class: "facts" }, facts),
    S.profile && pos?.furthest ? h("div", { class: "facts" }, n ? h("b", {}, `${n} new`) : "All caught up", n ? ` since #${series.chapters[indexOfChapter(series.chapters, pos.furthest)]?.n}` : "") : null,
    h("div", { class: "actions" },
      resume ? h("button", { class: "btn primary", onclick: () => playChapter(series.slug, resume.chapter.id, resume.t) },
        icon("play"), pos?.chapter ? `Continue · #${resume.chapter.n} ${formatTime(resume.t)}` : `Start · #${resume.chapter.n}`) : null,
      back ? h("button", { class: "btn", onclick: () => playChapter(series.slug, back.chapter.id, back.t) },
        `Back to #${back.chapter.n} · ${formatTime(back.t)}`) : null)));
  const rows = series.chapters.map((c) => {
    const st = chapterState(series.chapters, pos, c.id);
    return h("li", {}, h("button", {
      class: "row" + (S.now?.slug === series.slug && S.now.id === c.id ? " playing" : ""),
      "data-state": S.profile ? st : "none", "data-id": c.id,
      onclick: () => playChapter(series.slug, c.id, st === "current" && pos?.chapter === c.id ? pos.t : 0),
    }, h("span", { class: "mark" }), h("span", { class: "n" }, `#${c.n}`),
      h("span", { class: "tt" }, c.title), h("span", { class: "d" }, formatTime(c.dur))));
  });
  return h("div", {}, h("a", { class: "back", href: "#/" }, icon("back"), "Library"), hero,
    podcastPanel(series), h("ol", { class: "chapters" }, rows));
}

// The podcast feed for this series, on whichever address this page was opened from,
// so the home address gives a home feed and the tailnet name gives a tailnet one.
const feedUrl = (series) => `${location.origin}/feed/${encodeURIComponent(series.slug)}.xml`;

async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
  } catch { /* fall through */ }
  const box = h("textarea", { readonly: "", "aria-hidden": "true", style: "position:fixed;opacity:0;top:0" });
  box.value = text;
  document.body.append(box);                  // plain http has no clipboard api: select and copy
  box.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { /* blocked */ }
  box.remove();
  return ok;
}

function podcastPanel(series) {
  const url = feedUrl(series);
  const code = h("code", { class: "feedurl", tabindex: "0",
    onclick: () => { const r = document.createRange(); r.selectNodeContents(code); getSelection().removeAllRanges(); getSelection().addRange(r); } }, url);
  return h("details", { class: "podcast", open: S.podcastOpen, ontoggle: (e) => { S.podcastOpen = e.target.open; } },
    h("summary", {}, "Podcast feed"),
    h("p", { class: "dim" }, "To listen in a podcast app instead: use one that plays Opus (AntennaPod, Podcast Addict, gPodder). New chapters appear in the feed as they are rendered."),
    code,
    h("div", { class: "actions" },
      h("a", { class: "btn", href: `pcast://${url.split("://")[1]}` }, "Add to AntennaPod"),
      h("button", { class: "btn", onclick: async () => toast((await copyText(url)) ? "Feed URL copied" : "Select the URL to copy it") }, "Copy URL"),
      h("a", { class: "btn ghost", href: url }, "View feed")));
}

function scrollToCurrent() {
  const { slug } = route();
  const pos = slug && S.pos[slug];
  const row = pos && document.querySelector(`.row[data-id="${CSS.escape(pos.chapter || "")}"]`);
  if (row) row.scrollIntoView({ block: "center" }); else window.scrollTo(0, 0);
}

// -------------------------------------------------------------------- playback
const rate = () => Number(store.get(KEY.rate)) || 1;

function playChapter(slug, id, t = 0) {
  const series = S.by.get(slug), chapter = series?.chapters.find((c) => c.id === id);
  if (!chapter) return;
  savePosition();
  S.now = { slug, id };
  S.listened = 0; S.lastT = t;
  audio.src = chapter.audio;
  audio.defaultPlaybackRate = audio.playbackRate = rate();
  if (t > 0) audio.addEventListener("loadedmetadata", () => { audio.currentTime = t; }, { once: true });
  audio.play().catch(() => toast("Tap play to start"));
  noteProgress(0);
  setMediaSession(series, chapter);
  render();
}

/** Keep the in-memory position (and "furthest") current; saved on a timer. */
function noteProgress(t = audio.currentTime, finished = false) {
  if (!S.profile || !S.now) return;
  const series = S.by.get(S.now.slug);
  const pos = (S.pos[series.slug] ||= { chapter: null, t: 0, furthest: null, furthest_t: 0 });
  const far = advanceFurthest(series.chapters, pos.furthest, S.now.id, S.listened, finished);
  const moved = far !== pos.furthest;
  if (moved) { pos.furthest = far; pos.furthest_t = 0; }
  pos.chapter = S.now.id; pos.t = t; pos.updated = new Date().toISOString();
  if (pos.furthest === pos.chapter) pos.furthest_t = t;
  S.dirty = true;
  if (moved) render();                       // the "N new" counts changed
}

function step(dir) {
  if (!S.now) return;
  const series = S.by.get(S.now.slug);
  const target = (dir > 0 ? nextChapter : prevChapter)(series.chapters, S.now.id);
  if (target) playChapter(series.slug, target.id, 0); else toast(dir > 0 ? "That's the latest chapter" : "That's the first chapter");
}

const skip = (s) => { if (S.now) audio.currentTime = Math.min(Math.max(0, audio.currentTime + s), audio.duration || Infinity); };
const toggle = () => (audio.paused ? audio.play().catch(() => {}) : audio.pause());

audio.addEventListener("timeupdate", () => {
  const dt = audio.currentTime - S.lastT;
  if (dt > 0 && dt < 2) S.listened += dt;    // real listening, not a seek
  S.lastT = audio.currentTime;
  noteProgress();
  paintProgress();
  if (S.sleepAt && Date.now() >= S.sleepAt) { audio.pause(); S.sleepAt = 0; S.sleepIdx = 0; toast("Sleep timer: paused"); renderMini(); }
});
audio.addEventListener("play", () => paintPlayState());
audio.addEventListener("pause", () => {
  paintPlayState();
  savePosition(true);
  if (S.profile && S.now && !audio.ended) render();       // refresh "Continue \u00b7 #N m:ss" and the markers
});
audio.addEventListener("ended", () => {
  if (!S.now) return;
  const series = S.by.get(S.now.slug);
  noteProgress(audio.duration || audio.currentTime, true);
  const next = nextChapter(series.chapters, S.now.id);
  if (next) playChapter(series.slug, next.id, 0);
  else { savePosition(true); toast("You're all caught up"); paintPlayState(); }
});
audio.addEventListener("error", () => { if (S.now) toast("Couldn't play that chapter"); });

setInterval(() => { if (!audio.paused) savePosition(); }, 5000);
document.addEventListener("visibilitychange", () => { if (document.hidden) savePosition(true); else refresh(); });
window.addEventListener("pagehide", () => savePosition(true));
setInterval(() => { if (!document.hidden) refresh(); }, 5 * 60 * 1000);

function setMediaSession(series, chapter) {
  if (!("mediaSession" in navigator)) return;
  navigator.mediaSession.metadata = new MediaMetadata({
    title: chapter.title, artist: series.title, album: series.author || series.title,
    artwork: series.cover ? [{ src: new URL(series.cover, location.href).href, sizes: "512x512" }] : [],
  });
}
if ("mediaSession" in navigator) {
  const handlers = {
    play: () => audio.play(), pause: () => audio.pause(),
    previoustrack: () => step(-1), nexttrack: () => step(1),
    seekbackward: () => skip(-15), seekforward: () => skip(30),
    seekto: (d) => { if (d.seekTime != null) audio.currentTime = d.seekTime; },
  };
  for (const [action, fn] of Object.entries(handlers)) { try { navigator.mediaSession.setActionHandler(action, fn); } catch { /* unsupported */ } }
}

// ------------------------------------------------------------------ mini player
let dragging = false;
function renderMini() {
  const mini = $("#mini");
  if (!S.now) { mini.hidden = true; return; }
  const series = S.by.get(S.now.slug), chapter = series?.chapters.find((c) => c.id === S.now.id);
  if (!chapter) { mini.hidden = true; return; }
  mini.hidden = false;
  const seek = h("input", { type: "range", id: "seek", min: "0", max: "1000", value: "0", "aria-label": "Seek",
    oninput: () => { dragging = true; paintProgress(true); },
    onchange: () => { if (audio.duration) audio.currentTime = (seek.value / 1000) * audio.duration; dragging = false; } });
  mini.replaceChildren(
    h("a", { class: "now", href: `#/s/${encodeURIComponent(series.slug)}` },
      series.cover ? h("img", { class: "thumb", src: series.cover, alt: "" }) : h("div", { class: "thumb" }),
      h("div", { class: "txt" }, h("div", { class: "ct" }, `#${chapter.n} · ${chapter.title}`), h("div", { class: "cs" }, series.title))),
    h("div", { class: "scrub" }, h("span", { id: "cur" }, "0:00"), seek, h("span", { id: "dur" }, formatTime(chapter.dur))),
    h("div", { class: "ctl" },
      h("button", { class: "ib", "aria-label": "Previous chapter", onclick: () => step(-1) }, icon("prev")),
      h("button", { class: "ib", "aria-label": "Back 15 seconds", onclick: () => skip(-15) }, "−15"),
      h("button", { class: "ib play", id: "pp", "aria-label": "Play or pause", onclick: toggle }, icon("play")),
      h("button", { class: "ib", "aria-label": "Forward 30 seconds", onclick: () => skip(30) }, "+30"),
      h("button", { class: "ib", "aria-label": "Next chapter", onclick: () => step(1) }, icon("next"))),
    h("div", { class: "extra" },
      h("button", { class: "ib", id: "rate", "aria-label": "Playback speed", onclick: cycleRate }, `${rate()}×`),
      h("button", { class: "ib" + (S.sleepAt ? " on" : ""), id: "sleep", "aria-label": "Sleep timer", onclick: cycleSleep },
        S.sleepAt ? `${Math.max(1, Math.round((S.sleepAt - Date.now()) / 60000))}m` : "Sleep")));
  paintPlayState();
  paintProgress();
}

function paintPlayState() {
  const pp = $("#pp");
  if (pp) pp.replaceChildren(icon(audio.paused ? "play" : "pause"));
}

function paintProgress(fromSlider = false) {
  const seek = $("#seek");
  if (!seek) return;
  const dur = audio.duration || 0;
  if (!dragging || fromSlider) {
    if (!fromSlider) seek.value = dur ? Math.round((audio.currentTime / dur) * 1000) : 0;
    seek.style.setProperty("--p", `${seek.value / 10}%`);
  }
  const shown = fromSlider && dur ? (seek.value / 1000) * dur : audio.currentTime;
  $("#cur").textContent = formatTime(shown);
  if (dur) $("#dur").textContent = formatTime(dur);
  if (S.sleepAt && $("#sleep")) $("#sleep").textContent = `${Math.max(1, Math.round((S.sleepAt - Date.now()) / 60000))}m`;
  if ("mediaSession" in navigator && dur && Number.isFinite(dur)) {
    try { navigator.mediaSession.setPositionState({ duration: dur, position: Math.min(audio.currentTime, dur), playbackRate: audio.playbackRate }); } catch { /* not ready */ }
  }
}

function cycleRate() {
  const next = RATES[(RATES.indexOf(rate()) + 1) % RATES.length];
  store.set(KEY.rate, String(next));
  audio.defaultPlaybackRate = audio.playbackRate = next;
  $("#rate").textContent = `${next}×`;
}

function cycleSleep() {
  S.sleepIdx = (S.sleepIdx + 1) % SLEEPS.length;
  const m = SLEEPS[S.sleepIdx];
  S.sleepAt = m ? Date.now() + m * 60000 : 0;
  toast(m ? `Pausing in ${m} minutes` : "Sleep timer off");
  renderMini();
}

// --------------------------------------------------------------- profile picker
function setProfile(name) {
  if (name === S.profile) return;
  audio.pause();
  savePosition(true);
  S.now = null; S.dirty = false;
  S.profile = name;
  store.set(KEY.profile, name);
}

async function pickProfile() {
  const dlg = $("#who");
  let names = [];
  try { names = (await api("/api/profiles")).profiles; } catch { /* offline: just browsing */ }
  const err = h("div", { class: "err" });
  const choose = async (name) => {
    store.set(KEY.guest, "1");
    setProfile(name);
    dlg.close();
    await loadPositions();
    render();
  };
  const input = h("input", { name: "name", placeholder: "Add a name", maxlength: "24", autocomplete: "off", "aria-label": "New name" });
  const form = h("form", { class: "add", onsubmit: async (e) => {
    e.preventDefault();
    try { await choose((await api("/api/profiles", "POST", { name: input.value })).profile); }
    catch (ex) { err.textContent = ex.message; }
  } }, input, h("button", { class: "btn primary" }, "Add"));
  dlg.replaceChildren(...[
    h("h2", {}, "Who's listening?"),
    names.length ? h("div", { class: "chips" }, names.map((n) =>
      h("button", { class: "chip" + (n === S.profile ? " on" : ""), onclick: () => choose(n) }, avatar(n), n))) : null,
    form, err,
    h("button", { class: "btn ghost", onclick: () => choose(null) }, "Just browsing"),
  ].filter(Boolean));
  dlg.showModal();
}

$("#who-btn").addEventListener("click", pickProfile);
$("#who").addEventListener("click", (e) => { if (e.target === e.currentTarget) e.currentTarget.close(); });

// ----------------------------------------------------------------------- keys
document.addEventListener("keydown", (e) => {
  if (e.target.closest("input, textarea, dialog") || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === " " && e.target === document.body && S.now) { e.preventDefault(); toggle(); }
  else if (e.key === "ArrowLeft") skip(-15);
  else if (e.key === "ArrowRight") skip(30);
  else if (e.key === "n") step(1);
  else if (e.key === "p") step(-1);
});

window.addEventListener("hashchange", render);
render();
refresh().then(() => {
  if (!S.profile && !store.get(KEY.guest)) pickProfile();   // first visit: ask once
});
