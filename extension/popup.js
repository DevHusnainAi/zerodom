const urlInput = document.getElementById("url");
const actionBtn = document.getElementById("action");
const dot = document.querySelector(".dot");
const statusText = document.getElementById("status-text");

const STORAGE_KEY = "zerodom-relay-url";

function render(status) {
  statusText.textContent = status;
  dot.className = "dot " + status;
  if (status === "connected") {
    actionBtn.textContent = "Disconnect";
    actionBtn.className = "disconnect";
  } else {
    actionBtn.textContent = status === "connecting" ? "Connecting…" : "Connect";
    actionBtn.className = "";
  }
}

chrome.storage.local.get(STORAGE_KEY, (data) => {
  if (data[STORAGE_KEY]) urlInput.value = data[STORAGE_KEY];
});

chrome.runtime.sendMessage({ type: "zerodom-get-status" }, (res) => {
  if (res) render(res.status);
});

chrome.runtime.onMessage.addListener((message) => {
  if (message.type === "zerodom-status") render(message.status);
});

actionBtn.addEventListener("click", () => {
  if (actionBtn.textContent === "Disconnect") {
    chrome.runtime.sendMessage({ type: "zerodom-disconnect" });
    return;
  }
  const url = urlInput.value.trim();
  if (!url) return;
  chrome.storage.local.set({ [STORAGE_KEY]: url });
  chrome.runtime.sendMessage({ type: "zerodom-connect", url });
});
