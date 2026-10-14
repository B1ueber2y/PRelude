// PRelude: the little JavaScript the server-rendered pages need.
// - stream Claude's discussion replies (Server-Sent Events)
// - grow text areas with their content
// - focus the discussion box from the action bar

(function () {
  "use strict";

  let source = null;

  function autogrow(el) {
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight + 2, window.innerHeight * 0.5) + "px";
  }

  function closeStream() {
    if (source) {
      source.close();
      source = null;
    }
  }

  function refreshThread(findingId) {
    htmx.ajax("GET", "/findings/" + findingId + "/thread", { target: "#thread-" + findingId, swap: "innerHTML" });
  }

  function startStream(thread) {
    const findingId = thread.dataset.findingId;
    const live = thread.querySelector(".live-text");
    const activity = thread.querySelector(".activity-text");
    closeStream();
    source = new EventSource("/findings/" + findingId + "/stream");
    let text = "";
    source.addEventListener("text", function (e) {
      const chunk = JSON.parse(e.data);
      if (chunk === "") {
        if (text && !text.endsWith("\n")) text += "\n\n";
      } else {
        text += chunk;
      }
      if (live) live.textContent = text.replace(/```comment[\s\S]*?(```|$)/, "").replace(/\n?VERDICT:.*$/i, "");
      if (activity) activity.textContent = "Claude is writing…";
    });
    source.addEventListener("activity", function (e) {
      const tool = JSON.parse(e.data);
      const verbs = { Read: "Reading code…", Grep: "Searching the code…", Glob: "Looking for files…" };
      if (activity) activity.textContent = verbs[tool] || "Investigating…";
    });
    function finish() {
      closeStream();
      refreshThread(findingId);
    }
    source.addEventListener("done", finish);
    source.addEventListener("error", function (e) {
      // Server-sent "error" events carry data; connection errors don't.
      if (e.data !== undefined || source.readyState === EventSource.CLOSED) finish();
    });
  }

  function init(root) {
    root.querySelectorAll("textarea.autogrow").forEach(function (el) {
      autogrow(el);
      el.addEventListener("input", function () { autogrow(el); });
    });
    const thread = document.querySelector(".thread");
    if (thread && thread.dataset.busy === "1") startStream(thread);
  }

  document.addEventListener("DOMContentLoaded", function () { init(document); });
  document.addEventListener("htmx:load", function (e) { init(e.target); });
  document.addEventListener("htmx:beforeHistorySave", closeStream);

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

  // Send with Cmd/Ctrl+Enter from the discussion box.
  document.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && e.target.id === "composer-input") {
      e.preventDefault();
      htmx.trigger(e.target.form, "submit");
    }
  });
})();
