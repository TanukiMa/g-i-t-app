/* G醫t: progressive web app glue. Registers the service worker and offers "install as an app".
 * Pages work the same without any of this.
 *
 * Two modes (data-mode on the script tag, set by PWA_ENABLED at build time):
 *   "on"    the manifest is in every page; this file registers sw.js and shows the install UI.
 *   "optin" nothing is on for visitors. Whoever opens  <site>/?pwa  and types the passphrase turns the app on for THIS
 *           browser (a flag in localStorage); from then on the manifest is added to the page here, sw-app.js is
 *           registered and the install UI is shown. <site>/?pwa=off turns it off again. The passphrase is checked in
 *           the browser against a salted PBKDF2 hash (data-hash, from scripts/pwa-passphrase.py): it keeps the app
 *           from being switched on by accident, it is not access control (the site is public anyway). It is never put
 *           into the address (history and hosting logs would keep it).
 */
(function () {
  "use strict";

  var script = document.currentScript;
  var root = (script && script.getAttribute("data-root")) || "";
  var mode = (script && script.getAttribute("data-mode")) || "on";
  var hashSpec = (script && script.getAttribute("data-hash")) || "";
  var FLAG = "g-i-t-data:v1:pwa";   // same namespace as app.js

  function $$(selector) { return Array.prototype.slice.call(document.querySelectorAll(selector)); }
  function show(selector, visible) { $$(selector).forEach(function (el) { el.hidden = !visible; }); }

  function flagGet() { try { return window.localStorage.getItem(FLAG) === "1"; } catch (e) { return false; } }
  function flagSet(on) {
    try { if (on) window.localStorage.setItem(FLAG, "1"); else window.localStorage.removeItem(FLAG); } catch (e) { /* storage disabled */ }
  }

  // ---- install UI
  var standalone = (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) ||
    window.navigator.standalone === true;
  var isIos = /iphone|ipad|ipod/i.test(window.navigator.userAgent) ||
    (window.navigator.platform === "MacIntel" && window.navigator.maxTouchPoints > 1);
  var deferred = null;
  var active = mode === "on";        // in "optin" mode: true only after the flag has been found or the passphrase was right

  function refresh() {
    if (!active) { show("[data-install]", false); show("[data-install-ios]", false); show("[data-installed]", false); return; }
    show("[data-install]", !!deferred && !standalone);
    show("[data-install-ios]", isIos && !standalone && !deferred);
    show("[data-installed]", standalone);
  }

  window.addEventListener("beforeinstallprompt", function (event) {
    if (!active) return;                // not switched on in this browser: leave the browser's own behaviour alone
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
  var swFile = mode === "optin" ? "sw-app.js" : "sw.js";

  function registerWorker() {
    if (!("serviceWorker" in navigator) || !secure) return;
    var go = function () {
      navigator.serviceWorker.register(new URL(root + swFile, window.location.href)).catch(function () {
        /* offline support is optional */
      });
    };
    if (document.readyState === "complete") go(); else window.addEventListener("load", go);
  }

  // ---- opt-in: the manifest is added by script, only in a browser that was switched on
  function addHead(tag, attrs) {
    var el = document.createElement(tag);
    Object.keys(attrs).forEach(function (k) { el.setAttribute(k, attrs[k]); });
    document.head.appendChild(el);
  }

  function activate() {
    if (active && mode === "on") return;
    active = true;
    if (!document.querySelector('link[rel="manifest"]')) {
      addHead("link", { rel: "manifest", href: root + "manifest.webmanifest" });
      addHead("meta", { name: "mobile-web-app-capable", content: "yes" });
      addHead("meta", { name: "apple-mobile-web-app-title", content: "G醫t" });
    }
    registerWorker();
    refresh();
  }

  function deactivate() {
    active = false;
    deferred = null;
    $$('link[rel="manifest"]').forEach(function (el) { el.parentNode.removeChild(el); });
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.getRegistrations().then(function (list) {
        list.forEach(function (r) { r.unregister(); });
      }).catch(function () { /* nothing registered */ });
    }
    if (window.caches && caches.keys) {
      caches.keys().then(function (keys) {
        keys.forEach(function (k) { if (/^git-/.test(k)) caches.delete(k); });
      }).catch(function () { /* ignore */ });
    }
    refresh();
  }

  // ---- the passphrase: "pbkdf2$sha256$<iterations>$<salt base64>$<hash base64>"
  function b64ToBytes(s) {
    var bin = window.atob(s), out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  function checkPassphrase(phrase) {
    var parts = hashSpec.split("$");
    if (parts.length !== 5 || parts[0] !== "pbkdf2" || parts[1] !== "sha256" || !(window.crypto && window.crypto.subtle)) {
      return Promise.resolve(false);
    }
    var iterations = parseInt(parts[2], 10), salt, expected;
    try { salt = b64ToBytes(parts[3]); expected = b64ToBytes(parts[4]); } catch (e) { return Promise.resolve(false); }
    var subtle = window.crypto.subtle;
    return subtle.importKey("raw", new TextEncoder().encode(phrase.normalize("NFKC")), "PBKDF2", false, ["deriveBits"])
      .then(function (key) {
        return subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt: salt, iterations: iterations }, key, expected.length * 8);
      })
      .then(function (bits) {
        var got = new Uint8Array(bits), diff = got.length ^ expected.length;
        for (var i = 0; i < expected.length; i++) diff |= (got[i] || 0) ^ expected[i];   // no early exit
        return diff === 0;
      })
      .catch(function () { return false; });
  }

  function cleanAddress() {
    try {
      var url = new URL(window.location.href);
      url.searchParams.delete("pwa");
      window.history.replaceState(null, "", url.pathname + url.search + url.hash);
    } catch (e) { /* the address stays as it is */ }
  }

  function showPrompt() {
    var box = document.createElement("form");
    box.className = "pwa-unlock";
    box.setAttribute("aria-label", "アプリの有効化");
    var input = document.createElement("input");
    input.type = "password";
    input.autocomplete = "off";
    input.placeholder = "合言葉";
    input.setAttribute("aria-label", "合言葉");
    var ok = document.createElement("button");
    ok.type = "submit";
    ok.textContent = "アプリを有効にする";
    var msg = document.createElement("span");
    msg.className = "pwa-unlock-msg";
    msg.setAttribute("role", "status");
    box.appendChild(input); box.appendChild(ok); box.appendChild(msg);
    box.addEventListener("submit", function (event) {
      event.preventDefault();
      if (!input.value) return;
      msg.textContent = "確認しています…";
      checkPassphrase(input.value).then(function (good) {
        input.value = "";
        if (!good) { msg.textContent = "合言葉が違います。"; return; }
        flagSet(true);
        cleanAddress();
        activate();
        msg.textContent = "このブラウザでアプリを有効にしました。インストールのボタンが出ない場合は、ページを再読み込みしてください。";
        window.setTimeout(function () { if (box.parentNode) box.parentNode.removeChild(box); }, 6000);   // done: the box goes away
      });
    });
    document.body.appendChild(box);
    input.focus();
  }

  function startOptIn() {
    var params = new URLSearchParams(window.location.search);
    if (params.has("pwa")) {
      var value = params.get("pwa");
      if (value === "off") {
        flagSet(false);
        deactivate();
        cleanAddress();
        return;
      }
      if (standalone) {                 // inside the installed app: never ask, just show the normal address
        cleanAddress();
        if (flagGet()) activate();
        return;
      }
      if (!flagGet()) {
        if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", showPrompt); else showPrompt();
        return;
      }
      cleanAddress();                   // already on: just tidy the address
    }
    if (flagGet()) activate();
  }

  if (mode === "optin") startOptIn(); else registerWorker();
})();
