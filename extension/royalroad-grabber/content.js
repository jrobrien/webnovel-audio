// "Read the next N chapters": walks Next in this tab, as you would by hand, so
// Royal Road records your reading position as usual, and saves each page for
// `webnovel-audio ingest`. A page load, then a pause, then Next -- never faster
// than reading would be. It stops at N, at the last chapter, on anything that
// does not look like a chapter (a bot-check, say), or when you press Stop.

const KEY = "grabRun";
const PAUSE_MS = 4000;               // between chapters
const JITTER_MS = 2000;
const STALE_MS = 10 * 60 * 1000;     // an abandoned run does not resume itself
const CHAPTER = location.pathname.match(/^\/fiction\/(\d+)\/[^/]+\/chapter\/(\d+)/);
const SERIES = location.pathname.match(/^\/fiction\/(\d+)(?:\/[^/]+)?\/?$/);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const store = {
  get: async () => (await chrome.storage.local.get(KEY))[KEY],
  set: (run) => chrome.storage.local.set({ [KEY]: run }),
};

function panel() {
  let el = document.getElementById("wa-grab");
  if (el) return el;
  el = document.createElement("div");
  el.id = "wa-grab";
  el.style.cssText = "position:fixed;right:12px;bottom:12px;z-index:2147483647;" +
    "background:#222;color:#eee;font:13px/1.4 sans-serif;padding:8px 10px;" +
    "border-radius:6px;box-shadow:0 2px 8px #0008;display:flex;gap:8px;align-items:center";
  document.body.appendChild(el);
  return el;
}

function show(text, ...buttons) {
  const el = panel();
  el.replaceChildren();
  const span = document.createElement("span");
  span.textContent = text;
  el.append(span, ...buttons);
}

function button(label, onclick) {
  const b = document.createElement("button");
  b.textContent = label;
  b.style.cssText = "cursor:pointer;padding:2px 8px";
  b.onclick = onclick;
  return b;
}

// The page's own <link rel="next"> (absent on the last chapter) is what the
// Next button points at. The button's markup is not something to match on: its
// label is not even in the page text.
function nextLink() {
  const rel = document.querySelector("link[rel='next'][href*='/chapter/']");
  if (rel && rel.href !== location.href) return rel;
  return [...document.querySelectorAll("a[href*='/chapter/']")]
    .find((a) => /\bnext\b/i.test(a.textContent) && a.href !== location.href);
}

// The chapter text is in .chapter-content; a bot-check page has none.
async function chapterPresent() {
  for (let i = 0; i < 15; i++) {
    if (document.querySelector(".chapter-content")) return true;
    await sleep(1000);               // a challenge may clear itself and reload
  }
  return false;
}

function save(name = `rr-${CHAPTER[1]}-${CHAPTER[2]}.html`) {
  const html = "<!DOCTYPE html>\n" + document.documentElement.outerHTML;
  return chrome.runtime.sendMessage({ type: "save", name, html });
}

async function stop(run, why) {
  await store.set({ ...run, active: false });
  show(`${why} — ${run.done} saved`, button("Close", () => panel().remove()));
}

async function step(run) {
  if (!(await chapterPresent())) {
    return stop(run, "no chapter text on this page (a bot-check?)");
  }
  const saved = await save();
  if (!saved || !saved.ok) {
    return stop(run, `could not save: ${saved ? saved.error : "no reply"}`);
  }
  run = { ...run, done: run.done + 1, t: Date.now() };
  if (run.done >= run.n) return stop(run, "Done");
  const next = nextLink();
  if (!next) return stop(run, "No next chapter");

  await store.set(run);
  show(`Saved ${run.done} of ${run.n} · next in a moment`,
       button("Stop", () => stop(run, "Stopped")));
  await sleep(PAUSE_MS + Math.random() * JITTER_MS);
  const now = await store.get();
  if (!now || !now.active) return;   // Stop was pressed while waiting
  location.href = next.href;         // the page reload resumes the run
}

// The series page: its chapter list, and where Royal Road has you up to.
async function seriesPage() {
  const go = async () => {
    show("Saving the series page…");
    const saved = await save(`rr-fiction-${SERIES[1]}.html`);
    show(saved && saved.ok ? "Series page saved" : `could not save: ${saved ? saved.error : "no reply"}`,
         button("Close", () => panel().remove()));
  };
  if (location.hash.includes("wa-index")) {      // opened from the control UI
    history.replaceState(null, "", location.pathname + location.search);
    return go();
  }
  show("webnovel-audio:", button("Save series page", go));
}

// --- the loop led by `webnovel-audio grab` ---------------------------------
// The server decides everything: this sends each page it is given and asks what
// next. A run is remembered in storage because every chapter is a fresh page load.
const api = (run, method, path, body) =>
  chrome.runtime.sendMessage({ type: "api", port: run.port, token: run.token, method, path, body });

async function serverStop(run, why) {
  await store.set({ ...run, active: false });
  show(why, button("Close", () => panel().remove()));
}

const VERSION = chrome.runtime.getManifest().version;
const newerThan = (a, b) => {
  const x = a.split(".").map(Number), y = b.split(".").map(Number);
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    const d = (x[i] || 0) - (y[i] || 0);
    if (d) return d > 0;
  }
  return false;
};
// a button, when the server has a newer extension bundled than the one running
const updateButtons = (a) => (a && a.latest && newerThan(a.latest, VERSION))
  ? [button(`Update to ${a.latest}`, () => chrome.runtime.sendMessage({ type: "update" }))]
  : [];

// A run that finished cleanly closes its tab, unless the option says not to: a few
// seconds to see the result, with a button to keep the page.
async function serverDone(run, reason) {
  await store.set({ ...run, active: false });
  const { autoClose } = await chrome.storage.local.get("autoClose");
  if (autoClose === false) {
    return show(`Done: ${reason}`, button("Close", () => panel().remove()));
  }
  let keep = false;
  for (let left = 5; left > 0 && !keep; left--) {
    show(`Done: ${reason}. Closing this tab in ${left}…`,
         button("Keep open", () => { keep = true; show(`Done: ${reason}`, button("Close", () => panel().remove())); }));
    await sleep(1000);
  }
  if (keep) return;
  const r = await chrome.runtime.sendMessage({ type: "close" });
  if (!r || !r.ok) show(`Done: ${reason} (left open: ${r && r.reason || "could not close"})`);
}

// 426: the server and this extension speak different versions of the protocol.
const mismatch = (r) => r && r.status === 426
  ? (r.body.message === "reload the extension"
      ? "This extension is out of date: update it from the page webnovel-audio hosts (the log names it), then start again."
      : "This extension is newer than webnovel-audio: update webnovel-audio, then start again.")
  : "";

async function serverStep(run) {
  const kind = SERIES ? "series" : "chapter";
  if (kind === "chapter" && !(await chapterPresent())) {
    api(run, "POST", "/stop", {});
    return serverStop(run, "No chapter text on this page (a bot-check?). Stopped.");
  }
  show(`Sending this ${kind}…`, button("Stop", async () => {
    await api(run, "POST", "/stop", {});
    serverStop(run, "Stopped");
  }));
  const sent = await api(run, "POST", "/page", { kind, html: "<!DOCTYPE html>\n" + document.documentElement.outerHTML });
  if (mismatch(sent)) return serverStop(run, mismatch(sent));
  if (!sent || sent.status === 0) return serverStop(run, "webnovel-audio is not answering. Stopped.");
  if (!sent.ok || !sent.body.ok) {
    return serverStop(run, `Not accepted: ${sent.body && sent.body.detail || sent.status}. Stopped.`);
  }
  for (;;) {                                  // ask until the server says go
    const live = await store.get();
    if (!live || !live.active) return;        // Stop was pressed
    await store.set({ ...live, t: Date.now() });
    const nx = await api(run, "GET", "/next");
    if (mismatch(nx)) return serverStop(run, mismatch(nx));
    if (!nx || nx.status === 0) return serverStop(run, "webnovel-audio is not answering. Stopped.");
    const a = nx.body;
    if (a.action === "wait") {
      show(`Waiting: ${a.reason}`, button("Stop", async () => {
        await api(run, "POST", "/stop", {});
        serverStop(run, "Stopped");
      }), ...updateButtons(a));
      await sleep(5000);
      continue;
    }
    if (a.action === "fetch") {
      show(a.kind === "series" ? `Next series: ${a.title}` : `Next: #${a.number} ${a.title}`,
           ...updateButtons(a));
      await sleep(PAUSE_MS + Math.random() * JITTER_MS);
      location.href = a.url;                  // the reload resumes the run
      return;
    }
    if (a.clean) return serverDone(run, a.reason);
    return serverStop(run, `Done: ${a.reason}`);
  }
}

async function main() {
  const wa = location.hash.match(/wa-run=(\d+):([\w-]+)/);   // opened by `grab`
  if (wa) {
    history.replaceState(null, "", location.pathname + location.search);
    const fresh = { mode: "server", port: wa[1], token: wa[2], active: true, t: Date.now() };
    await store.set(fresh);
    return serverStep(fresh);
  }
  const live = await store.get();
  if (live && live.mode === "server" && live.active && Date.now() - live.t < STALE_MS) {
    return serverStep(live);
  }
  if (SERIES) return seriesPage();
  if (!CHAPTER) return;
  const run = await store.get();
  if (run && run.active && Date.now() - run.t < STALE_MS) {
    show(`Saving ${run.done + 1} of ${run.n}…`, button("Stop", () => stop(run, "Stopped")));
    return step(run);
  }
  const auto = location.hash.match(/wa-grab=(\d+)/);      // opened from the control UI
  if (auto) {
    history.replaceState(null, "", location.pathname + location.search);
    const fresh = { active: true, n: Math.max(1, Math.min(100, +auto[1])), done: 0, t: Date.now() };
    await store.set(fresh);
    return step(fresh);
  }
  const n = document.createElement("input");
  n.type = "number"; n.min = "1"; n.max = "100"; n.value = (run && run.n) || "10";
  n.style.cssText = "width:56px";
  show("Save this chapter and the next", n, document.createTextNode("chapters"),
       button("Start", () => {
         const count = Math.max(1, Math.min(100, parseInt(n.value, 10) || 1));
         const fresh = { active: true, n: count, done: 0, t: Date.now() };
         store.set(fresh).then(() => step(fresh));
       }));
}

main();
