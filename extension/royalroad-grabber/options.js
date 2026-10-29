// Settings live in the extension's own storage; nothing outside it needs to know.
const box = document.getElementById("autoClose");
chrome.storage.local.get("autoClose").then((r) => { box.checked = r.autoClose !== false; });
box.addEventListener("change", () => chrome.storage.local.set({ autoClose: box.checked }));
