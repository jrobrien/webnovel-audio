// On the extension page that webnovel-audio hosts on localhost: tell the page which
// version is installed, and let its "Update now" button reload the extension.
document.documentElement.dataset.waGrabberVersion = chrome.runtime.getManifest().version;
window.addEventListener("message", (e) => {
  if (e.source === window && e.data && e.data.waGrabber === "reload") {
    chrome.runtime.sendMessage({ type: "update" });
  }
});
