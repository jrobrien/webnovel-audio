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
