/* <order-widget>: a custom element whose shadow root only exists after
   upgrade. The served page calls window.__upgrade() itself; the
   upgrade-timing test in tests/test_playwright.py loads this logic inline
   and chooses when to upgrade, so parse-before-vs-after is deterministic. */
class OrderWidget extends HTMLElement {
  connectedCallback() {
    if (this.shadowRoot) return;
    var root = this.attachShadow({ mode: "open" });
    // <slot> renders the light-DOM decoy; the confirm button collides with it
    // label-for-label, which is the collision the parser must resolve.
    root.innerHTML =
      "<style>button{margin:2px 6px;padding:4px 10px}</style>" +
      '<slot></slot>' +
      '<button onclick="window.hits.push(\'shadow-confirm\')">Confirm order</button>' +
      '<div><button onclick="window.hits.push(\'shadow-discount\')">Apply discount</button>' +
      '<input name="po" placeholder="PO number"></div>';
  }
}
window.__upgrade = function () {
  if (!customElements.get("order-widget")) {
    customElements.define("order-widget", OrderWidget);
  }
};
window.__upgrade();
