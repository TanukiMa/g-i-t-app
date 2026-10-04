/* G医t: follow sites, filter the timeline. Everything stays in this browser (localStorage);
 * nothing is sent anywhere. Without JavaScript all updates are simply shown. */
(function () {
  "use strict";

  // GitHub Pages shares one origin (tanukima.github.io) between projects: namespace every key.
  var NS = "g-i-t-data:v1:";
  var store = {
    get: function (key, fallback) {
      try {
        var raw = window.localStorage.getItem(NS + key);
        return raw === null ? fallback : JSON.parse(raw);
      } catch (e) {
        return fallback;
      }
    },
    set: function (key, value) {
      try {
        window.localStorage.setItem(NS + key, JSON.stringify(value));
      } catch (e) {
        /* private mode / storage disabled: the page still works for this visit */
      }
    },
  };

  function $(selector, root) { return (root || document).querySelector(selector); }
  function $$(selector, root) { return Array.prototype.slice.call((root || document).querySelectorAll(selector)); }

  var followed = new Set(store.get("follow", []));
  var mode = store.get("mode", "all") === "followed" ? "followed" : "all";

  // ?follow=a,b,c : a followed-sites list shared by someone else. Shown read-only until the visitor keeps it.
  var params = new URLSearchParams(window.location.search);
  var sharedList = params.has("follow")
    ? params.get("follow").split(",").map(function (s) { return s.trim(); }).filter(function (s) { return /^[a-z0-9][a-z0-9_-]*$/.test(s); })
    : null;
  var shared = sharedList && sharedList.length ? new Set(sharedList) : null;

  var bar = $("#filter-bar");
  var qInput = $("#filter-q");
  var tagSelect = $("#filter-tag");
  var countLabel = $("#filter-count");
  var emptyNote = $("#filter-empty");
  var kind = bar ? bar.getAttribute("data-kind") : "timeline";

  function persist() { store.set("follow", Array.from(followed)); store.set("mode", mode); }

  function activeSet() { return shared || followed; }

  function apply() {
    var set = activeSet();
    var effective = shared ? "followed" : mode;
    var query = qInput ? qInput.value.trim().toLowerCase() : "";
    var tag = tagSelect ? tagSelect.value : "";
    var items = $$("[data-site]");
    var shown = 0;
    items.forEach(function (el) {
      var ok = effective === "all" || set.has(el.getAttribute("data-site"));
      if (ok && query) ok = (el.getAttribute("data-search") || "").indexOf(query) !== -1;
      if (ok && tag) ok = (el.getAttribute("data-tags") || "").split("|").indexOf(tag) !== -1;
      el.hidden = !ok;
      if (ok) shown++;
    });

    // Month pages: hide a day's heading and list when none of its rows match.
    $$("ul.row-list").forEach(function (list) {
      var any = $$("[data-site]", list).some(function (el) { return !el.hidden; });
      list.hidden = !any;
      var heading = list.previousElementSibling;
      if (heading && heading.classList.contains("day")) heading.hidden = !any;
    });

    $$("[data-follow]").forEach(function (btn) {
      var on = followed.has(btn.getAttribute("data-follow"));
      btn.setAttribute("aria-pressed", on ? "true" : "false");
      btn.textContent = on ? "★" : "☆";
      btn.title = on ? "フォロー中（クリックで解除）" : "フォローする";
    });

    if (!bar) return;
    $$("[data-mode]", bar).forEach(function (btn) {
      btn.setAttribute("aria-pressed", btn.getAttribute("data-mode") === effective ? "true" : "false");
    });
    var followCount = $("#follow-count");
    if (followCount) followCount.textContent = String(followed.size);
    if (countLabel) {
      countLabel.textContent = items.length
        ? shown + " / " + items.length + (kind === "sites" ? " サイト" : " 件") + "を表示中"
        : "";
    }
    if (emptyNote) emptyNote.hidden = shown !== 0 || items.length === 0;
  }

  function toggleFollow(slug) {
    if (followed.has(slug)) followed.delete(slug); else followed.add(slug);
    persist();
    apply();
  }

  function setBulk(add) {
    $$("[data-site]").forEach(function (el) {
      if (el.hidden) return;
      var slug = el.getAttribute("data-site");
      if (add) followed.add(slug); else followed.delete(slug);
    });
    persist();
    apply();
  }

  function shareUrl() {
    var url = new URL(window.location.href);
    url.search = "";
    url.hash = "";
    url.searchParams.set("follow", Array.from(followed).join(","));
    return url.toString();
  }

  function copy(text, done) {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { window.prompt("このリンクをコピーしてください:", text); });
    } else {
      window.prompt("このリンクをコピーしてください:", text);
    }
  }

  function showSharedBanner() {
    if (!shared) return;
    var banner = document.createElement("div");
    banner.className = "share-banner";
    banner.innerHTML = "<span></span> <button type=\"button\" data-act=\"keep\">自分のフォローに追加</button> " +
      "<button type=\"button\" data-act=\"close\">共有設定を閉じる</button>";
    $("span", banner).textContent = "共有されたフォロー設定（" + shared.size + " サイト）を表示中です。";
    var anchor = $("#content") || document.body;
    anchor.parentNode.insertBefore(banner, anchor);
    banner.addEventListener("click", function (ev) {
      var act = ev.target && ev.target.getAttribute("data-act");
      if (!act) return;
      if (act === "keep") {
        shared.forEach(function (slug) { followed.add(slug); });
        persist();
      }
      shared = null;
      banner.remove();
      var clean = new URL(window.location.href);
      clean.searchParams.delete("follow");
      window.history.replaceState(null, "", clean.toString());
      apply();
    });
  }

  function init() {
    // Controls need JavaScript, so they are hidden in the markup and revealed here.
    $$("[data-follow]").forEach(function (btn) {
      btn.hidden = false;
      btn.addEventListener("click", function () { toggleFollow(btn.getAttribute("data-follow")); });
    });

    if (bar) {
      bar.hidden = false;
      $$("[data-mode]", bar).forEach(function (btn) {
        btn.addEventListener("click", function () {
          mode = btn.getAttribute("data-mode") === "followed" ? "followed" : "all";
          if (shared) shared = null;
          persist();
          apply();
        });
      });
      if (qInput) qInput.addEventListener("input", apply);
      if (tagSelect) tagSelect.addEventListener("change", apply);
      var share = $("#share-btn");
      if (share) {
        share.addEventListener("click", function () {
          if (!followed.size) { share.textContent = "先にサイトをフォローしてください"; return; }
          copy(shareUrl(), function () {
            share.textContent = "✔ リンクをコピーしました";
            window.setTimeout(function () { share.textContent = "🔗 フォロー設定を共有"; }, 2500);
          });
        });
      }
      var all = $("#follow-visible"), none = $("#unfollow-visible");
      if (all) all.addEventListener("click", function () { setBulk(true); });
      if (none) none.addEventListener("click", function () { setBulk(false); });
    }

    showSharedBanner();
    apply();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
