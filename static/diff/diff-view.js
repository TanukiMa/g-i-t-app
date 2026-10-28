/* Draws a diff page: reads the raw git patch named in data-patch of #diff and lets diff2html (diff2html-ui.js, loaded
   before this file) show it side by side. The patch file is the record; this is only the view. */
(function () {
  "use strict";
  var target = document.getElementById("diff");
  var note = document.getElementById("diff-note");
  if (!target) return;

  function say(text) {
    if (note) { note.hidden = false; note.textContent = text; }
  }

  if (typeof Diff2HtmlUI !== "function") {
    say("差分の表示に必要なスクリプトを読み込めませんでした。下の「元の差分（テキスト）」を開いてください。");
    return;
  }

  fetch(target.getAttribute("data-patch"), { credentials: "same-origin" })
    .then(function (res) {
      if (!res.ok) throw new Error("HTTP " + res.status);
      return res.text();
    })
    .then(function (text) {
      var ui = new Diff2HtmlUI(target, text, { drawFileList: true, matching: "none", outputFormat: "side-by-side" });
      ui.draw();
      ui.fileListToggle(false);
      ui.fileContentToggle();
      ui.synchronisedScroll();
      ui.highlightCode();
      if (note) note.hidden = true;
    })
    .catch(function () {
      say("差分を読み込めませんでした。下の「元の差分（テキスト）」を開いてください。");
    });
})();
