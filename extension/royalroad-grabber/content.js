// The page grabber for `webnovel-audio grab`. It does one thing: load the page the
// server names, send its HTML back, show the server's status text. It keeps no
// state of its own; the server decides what to load next, how long to wait and
// when to stop.
//
// A run is the URL hash `#wa=<port>:<token>`, which the server puts on the first
// page it opens and this script puts on every page it is sent to next. No hash,
// no run: the script does nothing.

const PAUSE_MS = 4000;               // between pages: never faster than reading
const JITTER_MS = 2000;
const POLL_SECONDS = 20;             // how long the server may hold a /next request
const SETTLE_SECONDS = 15;           // wait for chapter text (a challenge may clear)
const VERSION = chrome.runtime.getManifest().version;
const SERIES = /^\/fiction\/\d+(?:\/[^/]+)?\/?$/.test(location.pathname);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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

function button(label, onclick) {
  const b = document.createElement("button");
  b.textContent = label;
  b.style.cssText = "cursor:pointer;padding:2px 8px";
  b.onclick = onclick;
  return b;
}

function show(text, ...buttons) {
  const span = document.createElement("span");
  span.textContent = text;
  panel().replaceChildren(span, ...buttons);
}

const newerThan = (a, b) => {
  const x = a.split(".").map(Number), y = b.split(".").map(Number);
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    const d = (x[i] || 0) - (y[i] || 0);
    if (d) return d > 0;
  }
  return false;
};

// 426: the server and this extension speak different protocol versions.
const mismatch = (r) => r && r.status === 426
  ? (r.body.message === "reload the extension"
      ? "This extension is out of date: update it from the page webnovel-audio hosts (the log names it), then start again."
      : "This extension is newer than webnovel-audio: update webnovel-audio, then start again.")
  : "";

async function run(port, token) {
  const hash = `#wa=${port}:${token}`;
  let stopped = false;
  const api = (method, path, body) =>
    chrome.runtime.sendMessage({ type: "api", port, token, method, path, body });
  const stopButton = () => button("Stop", async () => {
    stopped = true;
    await api("POST", "/stop", {});
    show("Stopped");
  });
  const end = (text) => show(text, button("Close", () => panel().remove()));

  if (!SERIES) {                                  // a chapter: let its text appear
    for (let i = 0; i < SETTLE_SECONDS && !document.querySelector(".chapter-content"); i++) {
      await sleep(1000);
    }
  }
  show("Sending this page…", stopButton());
  const sent = await api("POST", "/page", { html: "<!DOCTYPE html>\n" + document.documentElement.outerHTML });
  if (mismatch(sent)) return end(mismatch(sent));
  if (!sent || sent.status === 0) return end("webnovel-audio is not answering. Stopped.");
  if (!sent.ok || !sent.body.ok) {
    return end(`Not accepted: ${sent.body && sent.body.detail || sent.status}. Stopped.`);
  }

  while (!stopped) {                              // the server holds each request until it has an answer
    const nx = await api("GET", `/next?wait=${POLL_SECONDS}`);
    if (stopped) return;
    if (mismatch(nx)) return end(mismatch(nx));
    if (!nx || nx.status === 0) return end("webnovel-audio is not answering. Stopped.");
    const a = nx.body;
    const update = a.latest && newerThan(a.latest, VERSION)
      ? [button(`Update to ${a.latest}`, () => chrome.runtime.sendMessage({ type: "update" }))] : [];
    if (a.action === "wait") {
      show(a.say || "Waiting…", stopButton(), ...update);
    } else if (a.action === "fetch") {
      show(a.say || "Next page…", stopButton(), ...update);
      await sleep(PAUSE_MS + Math.random() * JITTER_MS);
      if (!stopped) {
        const target = a.url.split("#")[0] + hash;
        // the same address is only a fragment navigation, which loads nothing
        if (target === location.href) location.reload(); else location.href = target;
      }
      return;
    } else {
      return a.clean ? done(a.say || a.reason) : end(a.say || `Done: ${a.reason}`);
    }
  }
}

// A clean finish closes the tab, unless the option says not to: a few seconds to
// see the result, with a button to keep the page.
async function done(reason) {
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

const m = location.hash.match(/wa=(\d+):([\w-]+)/);
if (m) run(m[1], m[2]);
