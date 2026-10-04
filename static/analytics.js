/* G医t: access analytics (Google Analytics 4 and/or Cloudflare Web Analytics), opt-out style.
 *
 * - This file is only included in the pages when at least one ID is configured at build time
 *   (GA_MEASUREMENT_ID / CF_BEACON_TOKEN); the IDs arrive as data-ga / data-cf attributes.
 * - Measuring is ON by default and stops when the reader opts out on the privacy page, or when the
 *   browser sends Do Not Track / Global Privacy Control (an explicit "resume" on the privacy page wins).
 * - The query string never reaches the analytics services: ?follow=... carries the reader's list of
 *   followed sites. Google gets a cleaned URL; the Cloudflare beacon is not loaded for such a page view.
 */
(function () {
  "use strict";

  var script = document.currentScript;
  if (!script) return;
  var GA = script.getAttribute("data-ga") || "";
  var CF = script.getAttribute("data-cf") || "";
  var KEY = "g-i-t-data:v1:analytics"; // "off" = opted out, "on" = explicitly resumed

  function stored() {
    try { return window.localStorage.getItem(KEY); } catch (e) { return null; }
  }
  function store(value) {
    try {
      if (value === null) window.localStorage.removeItem(KEY); else window.localStorage.setItem(KEY, value);
    } catch (e) { /* storage disabled: the choice only lasts for this page view */ }
  }
  function browserSaysNo() {
    return navigator.doNotTrack === "1" || window.doNotTrack === "1" || navigator.globalPrivacyControl === true;
  }
  // Returns { on: bool, reason: "choice" | "browser" | "default" }
  function state() {
    var s = stored();
    if (s === "off") return { on: false, reason: "choice" };
    if (s === "on") return { on: true, reason: "choice" };
    if (browserSaysNo()) return { on: false, reason: "browser" };
    return { on: true, reason: "default" };
  }

  function cleanUrl() {
    return window.location.origin + window.location.pathname; // no query string, no hash
  }

  function addScript(src, attrs) {
    var el = document.createElement("script");
    el.src = src;
    Object.keys(attrs || {}).forEach(function (k) { el.setAttribute(k, attrs[k]); });
    document.head.appendChild(el);
  }

  function loadGoogle() {
    if (!GA) return;
    window.dataLayer = window.dataLayer || [];
    window.gtag = function () { window.dataLayer.push(arguments); };
    window.gtag("js", new Date());
    // page_location is fixed in the config so that every event (outbound clicks, scrolls, ...) carries the
    // cleaned URL, not only the page_view.
    window.gtag("config", GA, { send_page_view: false, page_location: cleanUrl() });
    window.gtag("event", "page_view", { page_location: cleanUrl(), page_title: document.title });
    addScript("https://www.googletagmanager.com/gtag/js?id=" + encodeURIComponent(GA), { async: "" });
  }

  function loadCloudflare() {
    if (!CF) return;
    if (new URLSearchParams(window.location.search).has("follow")) return;
    addScript("https://static.cloudflareinsights.com/beacon.min.js",
      { defer: "", "data-cf-beacon": JSON.stringify({ token: CF }) });
  }

  function clearGoogleCookies() {
    var host = window.location.hostname;
    document.cookie.split(";").forEach(function (c) {
      var name = c.split("=")[0].trim();
      if (name === "_ga" || name.indexOf("_ga_") === 0 || name === "_gid") {
        var past = "=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
        document.cookie = name + past;
        document.cookie = name + past + "; domain=" + host;
      }
    });
  }

  var current = state();
  if (current.on) {
    loadGoogle();
    loadCloudflare();
  }

  // Privacy page controls: <span data-analytics-status>, <button data-analytics-toggle>
  function render() {
    var s = state();
    var label = document.querySelector("[data-analytics-status]");
    var button = document.querySelector("[data-analytics-toggle]");
    if (label) {
      label.textContent = s.on
        ? "現在：計測しています。"
        : (s.reason === "browser"
          ? "現在：計測していません（お使いのブラウザの Do Not Track / Global Privacy Control の設定に従っています）。"
          : "現在：計測を停止しています。");
    }
    if (button) {
      button.textContent = s.on ? "アクセス解析を停止する" : "アクセス解析を再開する";
      button.setAttribute("aria-pressed", s.on ? "false" : "true");
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    var button = document.querySelector("[data-analytics-toggle]");
    if (button) {
      button.hidden = false;
      button.addEventListener("click", function () {
        if (state().on) { store("off"); clearGoogleCookies(); } else { store("on"); }
        render();
      });
    }
    render();
  });
})();
