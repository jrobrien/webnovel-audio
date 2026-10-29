// Saves a page into Downloads/webnovel-audio/. Downloads belong to the
// extension's service worker, so the content script hands pages over here.

function dataUrl(html) {
  const bytes = new TextEncoder().encode(html);
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return "data:text/html;charset=utf-8;base64," + btoa(bin);
}

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg.type !== "save") return;
  chrome.downloads.download({
    url: dataUrl(msg.html),
    filename: "webnovel-audio/" + msg.name,
    conflictAction: "overwrite",
    saveAs: false,
  }).then(
    (id) => reply({ ok: true, id }),
    (err) => reply({ ok: false, error: String(err && err.message || err) }),
  );
  return true;                       // reply asynchronously
});

// Must equal PROTOCOL in src/webnovel_audio/grab.py; the server refuses a mismatch.
const PROTOCOL = 1;

// The loop led by `webnovel-audio grab`: the content script cannot call the local
// server itself (a page's requests are bound by CORS), so it asks here.
chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg.type !== "api") return;
  fetch(`http://127.0.0.1:${msg.port}${msg.path}`, {
    method: msg.method,
    headers: { "X-Grab-Token": msg.token, "X-Grab-Protocol": String(PROTOCOL),
               "X-Grab-Ext": chrome.runtime.getManifest().version,
               "Content-Type": "application/json" },
    body: msg.body === undefined ? undefined : JSON.stringify(msg.body),
  }).then(
    async (r) => reply({ ok: r.ok, status: r.status, body: await r.json().catch(() => ({})) }),
    (err) => reply({ ok: false, status: 0, error: String(err && err.message || err) }),
  );
  return true;
});

// Close the tab that asked, once a run has finished cleanly. A tab is closed by its
// id, which needs no extra permission, and never the only tab in its window (that
// would close the window, and perhaps the browser).
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg.type !== "close" || !sender.tab) return;
  chrome.tabs.query({ windowId: sender.tab.windowId }).then((tabs) => {
    if (tabs.length <= 1) return reply({ ok: false, reason: "it is the only tab in its window" });
    return chrome.tabs.remove(sender.tab.id).then(() => reply({ ok: true }));
  }).catch((err) => reply({ ok: false, reason: String(err && err.message || err) }));
  return true;
});

// Update: reload this extension from its folder (an unpacked extension picks up new
// files that way), then refresh the tab that asked so its page runs the new code.
chrome.runtime.onMessage.addListener((msg, sender) => {
  if (msg.type !== "update") return;
  chrome.storage.local.set({ reloadTab: sender.tab ? sender.tab.id : null })
    .then(() => chrome.runtime.reload());
});

chrome.runtime.onInstalled.addListener(async () => {
  const { reloadTab } = await chrome.storage.local.get("reloadTab");
  if (reloadTab != null) {
    await chrome.storage.local.remove("reloadTab");
    chrome.tabs.reload(reloadTab).catch(() => {});
  }
});
