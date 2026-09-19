const actionBtn = document.getElementById("action");
const dot = document.querySelector(".dot");
const statusText = document.getElementById("status-text");

const DEFAULT_RELAY_URL = "ws://127.0.0.1:8765/extension/local";

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

chrome.runtime.sendMessage({ type: "zerodom-get-status" }, (res) => {
  if (res) render(res.status);
});

chrome.runtime.onMessage.addListener((message) => {
  if (message.type === "zerodom-status") render(message.status);
});

actionBtn.addEventListener("click", () => {
  if (actionBtn.textContent === "Disconnect") {
    chrome.runtime.sendMessage({ type: "zerodom-disconnect" });
  } else {
    chrome.runtime.sendMessage({ type: "zerodom-connect", url: DEFAULT_RELAY_URL });
  }
});
