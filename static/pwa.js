/* G醫t: progressive web app glue. Registers the service worker and offers "install as an app".
 * Pages work the same without any of this. Loaded only when the app is on (PWA_ENABLED unset or 1 at build time).
 */
(function () {
  "use strict";

  var script = document.currentScript;
  var root = (script && script.getAttribute("data-root")) || "";

  function $$(selector) { return Array.prototype.slice.call(document.querySelectorAll(selector)); }
  function show(selector, visible) { $$(selector).forEach(function (el) { el.hidden = !visible; }); }

  // ---- install UI
  var standalone = (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) ||
    window.navigator.standalone === true;
  var isIos = /iphone|ipad|ipod/i.test(window.navigator.userAgent) ||
    (window.navigator.platform === "MacIntel" && window.navigator.maxTouchPoints > 1);
  var deferred = null;

  function refresh() {
    show("[data-install]", !!deferred && !standalone);
    show("[data-install-ios]", isIos && !standalone && !deferred);
    show("[data-installed]", standalone);
  }

  window.addEventListener("beforeinstallprompt", function (event) {
    event.preventDefault();             // keep the prompt for our own button
    deferred = event;
    refresh();
  });
  window.addEventListener("appinstalled", function () {
    deferred = null;
    standalone = true;
    refresh();
  });

  document.addEventListener("DOMContentLoaded", function () {
    $$("[data-install-button]").forEach(function (button) {
      button.addEventListener("click", function () {
        if (!deferred) return;
        var prompt = deferred;
        deferred = null;
        prompt.prompt();
        if (prompt.userChoice) prompt.userChoice.then(refresh, refresh); else refresh();
      });
    });
    refresh();
  });

  // ---- service worker (needs https, or localhost for testing)
  var secure = window.location.protocol === "https:" || window.location.hostname === "localhost" ||
    window.location.hostname === "127.0.0.1";
  if (!("serviceWorker" in navigator) || !secure) return;
  var go = function () {
    navigator.serviceWorker.register(new URL(root + "sw.js", window.location.href)).catch(function () {
      /* offline support is optional */
    });
  };
  if (document.readyState === "complete") go(); else window.addEventListener("load", go);
})();
