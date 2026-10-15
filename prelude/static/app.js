// PRelude: the little JavaScript the server-rendered pages need.
// - stream Claude's discussion replies (Server-Sent Events), several threads at once
// - grow text areas with their content
// - focus the discussion box from the action bar

(function () {
  "use strict";

  const streams = {};  // thread URL -> its live reply stream

  function autogrow(el) {
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight + 2, window.innerHeight * 0.5) + "px";
  }

  function closeStream(base) {
    const s = streams[base];
    if (!s) return;
    s.source.close();
    clearInterval(s.watchdog);
    delete streams[base];
  }

  function closeStreams() {
    Object.keys(streams).forEach(closeStream);
  }

  // A thread (a finding's discussion, or one in a PR's discussion space) says where it lives:
  // data-base is its URL, data-target/data-swap where its fresh HTML goes.
  function refreshThread(thread) {
    const details = thread.closest("details");
    const open = details ? (details.open ? "?open=1" : "?open=0") : "";
    htmx.ajax("GET", thread.dataset.base + "/thread" + open, { target: thread.dataset.target, swap: thread.dataset.swap });
  }

  function startStream(thread) {
    const base = thread.dataset.base;
    const live = thread.querySelector(".live-text");
    const activity = thread.querySelector(".activity-text");
    closeStream(base);
    const source = new EventSource(base + "/stream");
    let text = "";
    source.addEventListener("text", function (e) {
      const chunk = JSON.parse(e.data);
      if (chunk === "") {
        if (text && !text.endsWith("\n")) text += "\n\n";
      } else {
        text += chunk;
      }
      // The suggested wording and verdict are shown properly once the reply is complete.
      if (live) live.textContent = text.split("```comment")[0].replace(/\n?VERDICT:.*$/i, "").trim();
      if (activity) activity.textContent = "Claude is writing…";
    });
    source.addEventListener("activity", function (e) {
      const tool = JSON.parse(e.data);
      const verbs = { Read: "Reading code…", Grep: "Searching the code…", Glob: "Looking for files…" };
      if (activity) activity.textContent = verbs[tool] || "Investigating…";
    });
    let finished = false;
    function finish() {
      if (finished) return;
      finished = true;
      closeStream(base);
      refreshThread(thread);
    }
    // Don't depend only on the stream's last event: it can get lost on the way to a phone.
    const watchdog = setInterval(function () {
      fetch(base + "/busy", { credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (d) { if (!d.busy) finish(); })
        .catch(function () {});
    }, 3000);
    streams[base] = { source: source, watchdog: watchdog };
    source.addEventListener("done", finish);
    source.addEventListener("error", function (e) {
      // Server-sent "error" events carry data; connection errors don't.
      if (e.data !== undefined || source.readyState === EventSource.CLOSED) finish();
    });
  }

  function streamBusyThreads() {
    document.querySelectorAll('.thread[data-busy="1"]').forEach(function (thread) {
      if (!streams[thread.dataset.base]) startStream(thread);
    });
  }

  function init(root) {
    root.querySelectorAll("textarea.autogrow").forEach(function (el) {
      autogrow(el);
      el.addEventListener("input", function () { autogrow(el); });
    });
    streamBusyThreads();
    const selected = root.querySelector(".frow.selected");  // laptop split view: show it in the list
    if (selected) selected.scrollIntoView({ block: "nearest" });
  }

  // In-app navigation only swaps the page body, so new CSS/JS would never load:
  // reload fully when the server's asset versions differ from the ones we started with.
  let assets = null;
  function checkAssets() {
    const el = document.getElementById("assets");
    if (!el) return;
    if (assets === null) assets = el.dataset.v;
    else if (el.dataset.v !== assets) window.location.reload();
  }

  document.addEventListener("DOMContentLoaded", function () { checkAssets(); init(document); });
  document.addEventListener("htmx:afterSettle", checkAssets);
  document.addEventListener("htmx:load", function (e) { init(e.target); });
  document.addEventListener("htmx:beforeHistorySave", closeStreams);

  // Coming back to the app (e.g. from Termius): pick up a reply that finished meanwhile.
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState !== "visible") return;
    closeStreams();  // the phone may have cut them while away
    streamBusyThreads();
  });

  document.addEventListener("click", function (e) {
    const link = e.target.closest("[data-focus]");
    if (!link) return;
    const input = document.getElementById(link.dataset.focus);
    if (input) {
      e.preventDefault();
      input.scrollIntoView({ behavior: "smooth", block: "center" });
      input.focus({ preventScroll: true });
    }
  });

  // Laptop split view: keep the selected row in sync with the pane, and keyboard shortcuts.
  function markSelected() {
    const pane = document.querySelector("#pane .finding");
    const id = pane ? pane.dataset.findingId : null;
    document.querySelectorAll(".frow").forEach(function (row) {
      row.classList.toggle("selected", row.dataset.findingId === id);
    });
  }
  document.addEventListener("htmx:afterSettle", markSelected);
  document.addEventListener("htmx:historyRestore", markSelected);

  document.addEventListener("keydown", function (e) {
    if (!document.getElementById("pane") || e.metaKey || e.ctrlKey || e.altKey) return;
    const pane = document.getElementById("pane");
    if (e.key === "Escape") {
      const cancel = pane.querySelector('[data-key="esc"]');
      const typing = e.target.closest("input, textarea, select, [contenteditable]");
      if (e.isComposing || !cancel || (typing && !cancel.form.contains(e.target))) return;
      e.preventDefault();
      cancel.click();
      return;
    }
    if (e.target.closest("input, textarea, select, [contenteditable]")) return;
    const rows = Array.from(document.querySelectorAll(".frow"));
    if (e.key === "j" || e.key === "k") {
      const at = rows.findIndex(function (r) { return r.classList.contains("selected"); });
      const next = rows[e.key === "j" ? at + 1 : Math.max(0, at - 1)];
      if (next && next !== rows[at]) {
        e.preventDefault();
        rows.forEach(function (r) { r.classList.toggle("selected", r === next); });  // before the pane arrives
        next.click();
        next.scrollIntoView({ block: "nearest" });
      }
      return;
    }
    const button = pane.querySelector('[data-key="' + e.key + '"]');
    if (button && !button.disabled) { e.preventDefault(); button.click(); }
  });

  // Send with Cmd/Ctrl+Enter from a discussion box.
  document.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && e.target.closest("form.composer")) {
      e.preventDefault();
      htmx.trigger(e.target.form, "submit");
    }
  });
})();
