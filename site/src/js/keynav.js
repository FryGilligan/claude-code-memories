// Vim-style keyboard navigation for the memories site.
//   j / k         next / prev day with memories (calendar pages only)
//   o / Enter     open the focused day
//   gg / G        first / last day with memories
//   /             go to search
//   ?             toggle help overlay
//   Esc           close help overlay
(function () {
  "use strict";

  const SHORTCUTS = [
    ["j", "next day with memories"],
    ["k", "previous day with memories"],
    ["o / Enter", "open focused day"],
    ["g g", "jump to first day"],
    ["G", "jump to last day"],
    ["/", "go to search"],
    ["?", "toggle this help"],
    ["Esc", "close help"],
  ];

  let focusIdx = -1;
  let lastG = 0;
  let helpEl = null;

  const days = () => Array.from(document.querySelectorAll(".cal-cell.has-memory"));

  function setFocus(idx) {
    const list = days();
    if (!list.length) return;
    list.forEach((d) => d.classList.remove("keynav-focus"));
    focusIdx = ((idx % list.length) + list.length) % list.length;
    const el = list[focusIdx];
    el.classList.add("keynav-focus");
    el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function openFocused() {
    const list = days();
    if (!list.length || focusIdx < 0) return;
    const link = list[focusIdx].querySelector(".day-link");
    if (link) window.location.href = link.getAttribute("href");
  }

  function buildHelp() {
    const wrap = document.createElement("div");
    wrap.className = "key-help";
    wrap.innerHTML =
      '<div class="key-help-card">' +
      '<div class="key-help-title">keyboard shortcuts</div>' +
      '<dl class="key-help-list">' +
      SHORTCUTS.map(
        ([k, d]) =>
          '<div class="key-help-row"><dt><kbd>' + k.replace(/ /g, "</kbd> <kbd>") + "</kbd></dt>" +
          "<dd>" + d + "</dd></div>"
      ).join("") +
      "</dl>" +
      '<div class="key-help-foot">press <kbd>?</kbd> or <kbd>Esc</kbd> to close</div>' +
      "</div>";
    wrap.addEventListener("click", (e) => {
      if (e.target === wrap) closeHelp();
    });
    document.body.appendChild(wrap);
    return wrap;
  }

  function toggleHelp() {
    if (helpEl) { closeHelp(); return; }
    helpEl = buildHelp();
  }
  function closeHelp() {
    if (helpEl) { helpEl.remove(); helpEl = null; }
  }

  document.addEventListener("keydown", (e) => {
    // Don't hijack typing in inputs, search box, etc.
    const t = e.target;
    if (t && (t.matches("input, textarea, select, [contenteditable=true]") ||
              t.closest(".pagefind-ui"))) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;

    switch (e.key) {
      case "?":
        e.preventDefault(); toggleHelp(); return;
      case "Escape":
        if (helpEl) { e.preventDefault(); closeHelp(); }
        return;
      case "/":
        e.preventDefault();
        if (window.location.pathname !== "/search/") window.location.href = "/search/";
        return;
      case "j":
        e.preventDefault();
        setFocus(focusIdx < 0 ? 0 : focusIdx + 1); return;
      case "k":
        e.preventDefault();
        setFocus(focusIdx < 0 ? days().length - 1 : focusIdx - 1); return;
      case "o":
      case "Enter":
        if (focusIdx >= 0) { e.preventDefault(); openFocused(); }
        return;
      case "g":
        if (Date.now() - lastG < 500) { e.preventDefault(); setFocus(0); lastG = 0; return; }
        lastG = Date.now();
        return;
      case "G":
        e.preventDefault(); setFocus(days().length - 1); return;
    }
  });

  // Click-to-navigate on heatmap cells (links via data-href).
  document.addEventListener("click", (e) => {
    const cell = e.target.closest(".hm-cell[data-href]");
    if (cell) window.location.href = cell.getAttribute("data-href");
  });
})();
