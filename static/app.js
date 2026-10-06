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

  // ---- full-text search over every update summary (search.json, loaded on first use) ----
  var ROOT = (document.currentScript && document.currentScript.getAttribute("data-root")) || "";
  var MAX_HITS = 50;
  var index = null, indexPromise = null;

  function norm(text) { return String(text || "").normalize("NFKC").toLowerCase(); }

  function loadIndex() {
    if (!indexPromise) {
      indexPromise = fetch(ROOT + "search.json").then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      }).then(function (data) {
        var names = {};
        data.sites.forEach(function (s) { names[s[0]] = s[1]; });
        index = {
          names: names,
          sites: data.sites.map(function (s) {
            return { slug: s[0], name: s[1], url: s[2], tags: s[3], hay: norm([s[1], s[2], s[0], s[3].join(" ")].join(" ")) };
          }),
          updates: data.updates.map(function (u) {
            return { slug: u[0], date: u[1], hash: u[2], text: u[3], diff: u[4],
                     hay: norm(u[3] + " " + (names[u[0]] || u[0]) + " " + u[1]) };
          }),
        };
        return index;
      });
      indexPromise.catch(function () { indexPromise = null; });
    }
    return indexPromise;
  }

  function terms(query) { return norm(query).split(/\s+/).filter(Boolean); }
  function matches(hay, ts) { return ts.every(function (t) { return hay.indexOf(t) !== -1; }); }

  // Text with every search term wrapped in <mark>, built with DOM nodes only (nothing is parsed as HTML).
  function highlight(parent, text, ts) {
    var low = norm(text);
    if (low.length !== text.length) { parent.appendChild(document.createTextNode(text)); return; }  // NFKC changed lengths
    var marks = new Array(text.length + 1).join("0").split("");
    ts.forEach(function (t) {
      for (var i = low.indexOf(t); i !== -1; i = low.indexOf(t, i + t.length)) {
        for (var k = i; k < i + t.length; k++) marks[k] = "1";
      }
    });
    var i = 0;
    while (i < text.length) {
      var on = marks[i] === "1", j = i;
      while (j < text.length && (marks[j] === "1") === on) j++;
      var piece = text.slice(i, j);
      if (on) { var m = document.createElement("mark"); m.textContent = piece; parent.appendChild(m); }
      else parent.appendChild(document.createTextNode(piece));
      i = j;
    }
  }

  function snippet(text, ts) {
    var low = norm(text), at = -1;
    if (low.length === text.length) ts.forEach(function (t) { var p = low.indexOf(t); if (p !== -1 && (at === -1 || p < at)) at = p; });
    var start = Math.max(0, (at === -1 ? 0 : at) - 40);
    return (start > 0 ? "…" : "") + text.slice(start, start + 160) + (start + 160 < text.length ? "…" : "");
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function renderResults(box, query) {
    var ts = terms(query);
    box.textContent = "";
    if (!ts.length) { box.hidden = true; return; }
    box.hidden = false;
    var siteHits = index.sites.filter(function (s) { return matches(s.hay, ts); });
    var hits = index.updates.filter(function (u) { return matches(u.hay, ts); });   // newest first
    if (!siteHits.length && !hits.length) { box.appendChild(el("p", "gsearch-empty", "見つかりませんでした。")); return; }

    siteHits.slice(0, 5).forEach(function (s) {
      var a = el("a", "gsearch-site");
      a.href = ROOT + "sites/" + s.slug + "/history.html";
      highlight(a, s.name, ts);
      a.appendChild(el("span", "muted", "  サイト"));
      box.appendChild(a);
    });
    box.appendChild(el("p", "gsearch-count", "更新 " + hits.length + " 件" + (hits.length > MAX_HITS ? "（新しい順に " + MAX_HITS + " 件を表示）" : "")));
    hits.slice(0, MAX_HITS).forEach(function (u) {
      var item = el("div", "gsearch-hit");
      var head = el("div", "gsearch-head");
      head.appendChild(el("time", "muted", u.date));
      var a = el("a", "site-name");
      a.href = ROOT + "sites/" + u.slug + "/history.html";
      highlight(a, index.names[u.slug] || u.slug, ts);
      head.appendChild(a);
      if (u.diff) {
        var d = el("a", "row-link", "差分");
        d.href = ROOT + "sites/" + u.slug + "/" + u.diff;
        head.appendChild(d);
      }
      item.appendChild(head);
      var p = el("p", "gsearch-text");
      highlight(p, snippet(u.text, ts), ts);
      item.appendChild(p);
      box.appendChild(item);
    });
  }

  function initSearch() {
    var main = $("main");
    if (!main || !window.fetch) return;
    var wrap = el("div", "gsearch");
    var input = el("input");
    input.type = "search";
    input.placeholder = "全文検索（要約・サイト名）— 「/」で移動";
    input.setAttribute("aria-label", "全文検索");
    input.autocomplete = "off";
    var box = el("div", "gsearch-results");
    box.hidden = true;
    box.setAttribute("aria-live", "polite");
    wrap.appendChild(input);
    wrap.appendChild(box);
    main.insertBefore(wrap, main.firstChild);

    var timer = null;
    function run() {
      var q = input.value;
      if (!terms(q).length) { box.hidden = true; box.textContent = ""; return; }
      box.hidden = false;
      if (!index) box.textContent = "読み込み中…";
      loadIndex().then(function () { if (input.value === q) renderResults(box, q); })
        .catch(function () { box.textContent = "検索データを読み込めませんでした（オフライン？）。"; });
    }
    input.addEventListener("input", function () { window.clearTimeout(timer); timer = window.setTimeout(run, 120); });
    input.addEventListener("focus", function () { loadIndex().catch(function () {}); });
    input.addEventListener("keydown", function (e) { if (e.key === "Escape") { input.value = ""; run(); input.blur(); } });
    document.addEventListener("keydown", function (e) {
      var tag = (document.activeElement && document.activeElement.tagName) || "";
      if (e.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test(tag) && !e.ctrlKey && !e.metaKey) {
        e.preventDefault();
        input.focus();
      }
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

    initSearch();
    showSharedBanner();
    apply();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
