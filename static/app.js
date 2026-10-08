/* G醫t: follow sites, filter the timeline. Everything stays in this browser (localStorage);
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
  var tagSelect = $("#filter-tag");
  var countLabel = $("#filter-count");
  var emptyNote = $("#filter-empty");
  var kind = bar ? bar.getAttribute("data-kind") : "timeline";

  function persist() { store.set("follow", Array.from(followed)); store.set("mode", mode); }

  function activeSet() { return shared || followed; }

  function apply() {
    var set = activeSet();
    var effective = shared ? "followed" : mode;
    var tag = tagSelect ? tagSelect.value : "";
    var items = $$("[data-site]");
    var shown = 0;
    items.forEach(function (el) {
      var ok = effective === "all" || set.has(el.getAttribute("data-site"));
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
  var MAX_HITS = 50, MAX_SITES = 20;
  var index = null, indexPromise = null;

  function norm(text) { return String(text || "").normalize("NFKC").toLowerCase(); }

  function loadIndex() {
    if (!indexPromise) {
      indexPromise = fetch(ROOT + "search.json").then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      }).then(function (data) {
        var names = {}, urls = {};
        data.sites.forEach(function (s) { names[s[0]] = s[1]; urls[s[0]] = s[2] || ""; });
        index = {
          names: names,
          sites: data.sites.map(function (s) {
            return { slug: s[0], name: s[1], url: s[2], tags: s[3], hay: norm([s[1], s[2], s[0], s[3].join(" ")].join(" ")) };
          }),
          updates: data.updates.map(function (u) {
            return { slug: u[0], date: u[1], hash: u[2], text: u[3], diff: u[4],
                     hay: norm(u[3] + " " + (names[u[0]] || u[0]) + " " + (urls[u[0]] || "") + " " + u[1]),
                     key: u[1].slice(0, 10) + " " + u[1].slice(-5) };
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

  function hostOf(url) {
    var m = /^https?:\/\/(?:www\.)?([^\/?#]+)/i.exec(url || "");
    return m ? m[1] : "";
  }

  // ---- saved keywords: kept in localStorage only ([{q, seen}]); "seen" is the newest update (date + time) already looked at ----
  var keywords = store.get("keywords", []);
  if (!Array.isArray(keywords)) keywords = [];
  keywords = keywords.filter(function (k) { return k && typeof k.q === "string" && k.q.trim(); });
  function saveKeywords() { store.set("keywords", keywords); }
  function kwId(q) { return terms(q).join(" "); }
  function findKeyword(q) { var id = kwId(q); return keywords.filter(function (k) { return kwId(k.q) === id; })[0] || null; }
  function hitsFor(q) {
    var ts = terms(q);
    return ts.length ? index.updates.filter(function (u) { return matches(u.hay, ts); }) : [];
  }
  function newestKey() { return index && index.updates.length ? index.updates.reduce(function (m, u) { return u.key > m ? u.key : m; }, "") : ""; }

  function renderResults(box, query) {
    var ts = terms(query);
    var saved = findKeyword(query);
    var seenBefore = saved ? saved.seen : null;
    box.textContent = "";
    if (!ts.length) { box.hidden = true; return; }
    box.hidden = false;
    var siteHits = index.sites.filter(function (s) { return matches(s.hay, ts); });
    var hits = index.updates.filter(function (u) { return matches(u.hay, ts); });   // newest first
    if (!siteHits.length && !hits.length) { box.appendChild(el("p", "gsearch-empty", "見つかりませんでした。")); return; }

    siteHits.slice(0, MAX_SITES).forEach(function (s) {
      var a = el("a", "gsearch-site");
      a.href = ROOT + "sites/" + s.slug + "/history.html";
      highlight(a, s.name, ts);
      a.appendChild(el("span", "muted", "  " + hostOf(s.url)));
      box.appendChild(a);
    });
    if (siteHits.length > MAX_SITES) box.appendChild(el("p", "gsearch-count", "ほか " + (siteHits.length - MAX_SITES) + " サイト（語を足して絞り込んでください）"));
    box.appendChild(el("p", "gsearch-count", "更新 " + hits.length + " 件" + (hits.length > MAX_HITS ? "（新しい順に " + MAX_HITS + " 件を表示）" : "")));
    hits.slice(0, MAX_HITS).forEach(function (u) {
      var item = el("div", "gsearch-hit");
      var head = el("div", "gsearch-head");
      if (seenBefore !== null && u.key > seenBefore) head.appendChild(el("span", "gsearch-new", "NEW"));
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
    if (saved && hits.length && hits[0].key > saved.seen) {   // looked at: the next visit counts only newer updates
      saved.seen = hits[0].key;
      saveKeywords();
      if (box.onSeen) box.onSeen();
    }
  }

  function initSearch() {
    var main = $("main");
    if (!main || !window.fetch) return;
    var wrap = el("div", "gsearch");
    var input = el("input");
    input.type = "search";
    input.placeholder = "サイト名・ドメイン・要約を検索 — 「/」で移動";
    input.setAttribute("aria-label", "全文検索");
    input.autocomplete = "off";
    var box = el("div", "gsearch-results");
    box.hidden = true;
    box.setAttribute("aria-live", "polite");
    var kwBar = el("div", "gsearch-kw");
    var saveBtn = el("button", "gsearch-save", "★ この語を保存");
    saveBtn.type = "button";
    saveBtn.hidden = true;
    var inputRow = el("div", "gsearch-row");
    inputRow.appendChild(input);
    inputRow.appendChild(saveBtn);
    wrap.appendChild(inputRow);
    wrap.appendChild(kwBar);
    wrap.appendChild(box);
    var status = $("#status", main);   // the line ending in "G醫tについて": the search box goes on the next row
    var anchor = status && (status.closest("header") || status);   // #status is the last line of the page header
    if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(wrap, anchor.nextSibling);
    else main.insertBefore(wrap, main.firstChild);

    function syncSave() {
      var has = terms(input.value).length > 0;
      saveBtn.hidden = !has || !!findKeyword(input.value);
    }
    function renderKeywords() {
      kwBar.textContent = "";
      kwBar.hidden = !keywords.length;
      keywords.forEach(function (k) {
        var chip = el("span", "gsearch-chip");
        var open = el("button", "gsearch-chip-q", "★ " + k.q);
        open.type = "button";
        open.title = "この語で検索";
        open.addEventListener("click", function () { input.value = k.q; run(); });
        chip.appendChild(open);
        if (index) {
          var n = hitsFor(k.q).filter(function (u) { return u.key > k.seen; }).length;
          if (n) chip.appendChild(el("span", "gsearch-badge", String(n)));
        }
        var del = el("button", "gsearch-chip-x", "×");
        del.type = "button";
        del.title = "保存した語を削除";
        del.setAttribute("aria-label", k.q + " を削除");
        del.addEventListener("click", function () {
          keywords = keywords.filter(function (x) { return x !== k; });
          saveKeywords(); renderKeywords(); syncSave(); run();
        });
        chip.appendChild(del);
        kwBar.appendChild(chip);
      });
    }
    box.onSeen = renderKeywords;
    saveBtn.addEventListener("click", function () {
      var q = input.value.trim();
      if (!terms(q).length || findKeyword(q)) return;
      loadIndex().then(function () {
        // saved now: only updates detected from here on count as new (the hits already shown are the current state)
        keywords.push({ q: q, seen: newestKey() });
        saveKeywords(); renderKeywords(); syncSave(); run();
      }).catch(function () {});
    });
    renderKeywords();
    if (keywords.length && window.fetch) loadIndex().then(renderKeywords).catch(function () {});

    var timer = null;
    function run() {
      var q = input.value;
      syncSave();
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

  // ---- visual timeline of a site's updates (history page): an SVG drawn here from the cards below it ----
  // Nothing is stored anywhere: the points come from the cards' data attributes (hash, day, summary excerpt).
  var SVGNS = "http://www.w3.org/2000/svg";
  var GTL_FIRST = 200;   // newest points drawn at first; a button draws all of them (there is no upper limit)

  function svgEl(tag, attrs, text) {
    var e = document.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function initTimeline() {
    var host = $("#gtl");
    if (!host) return;
    var cards = $$("#content .entry[data-commit]");
    if (cards.length < 1) return;
    var DAY = 86400000, WEEK = 7 * DAY;
    var DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    function pad2(n) { return (n < 10 ? "0" : "") + n; }

    // Every card carries its JST day and time. Dates are handled as if they were UTC: only differences matter.
    var items = [];
    cards.forEach(function (c, order) {
      var d = /^(\d{4})-(\d{2})-(\d{2})$/.exec(c.getAttribute("data-day") || "");
      var t = /^(\d{2}):(\d{2})$/.exec(c.getAttribute("data-time") || "");
      if (!d || !t) return;
      var ms = Date.UTC(+d[1], +d[2] - 1, +d[3], +t[1], +t[2]);
      var dayStart = Date.UTC(+d[1], +d[2] - 1, +d[3]);
      var sinceMonday = (new Date(dayStart).getUTCDay() + 6) % 7;
      items.push({ id: c.id, hash: c.getAttribute("data-commit"), day: d[1] + "/" + d[2] + "/" + d[3], time: t[1] + ":" + t[2],
                   text: c.getAttribute("data-excerpt") || "", initial: c.classList.contains("entry--initial"),
                   ms: ms, week: dayStart - sinceMonday * DAY, order: order });
    });
    if (items.length < 1) return;
    items.sort(function (a, b) { return a.ms - b.ms || b.order - a.order; });   // oldest first (cards are newest first)
    var all = items;
    var showAll = all.length <= GTL_FIRST;
    items = showAll ? all : all.slice(all.length - GTL_FIRST);

    var PADX = 18, MIN_W = 760, FLAG_W = 76, FLAG_H = 42, HEAD = 36, TIMER = null, lastWidth = 0;

    function draw() {
      var width = Math.max(MIN_W, host.clientWidth || MIN_W);
      lastWidth = host.clientWidth || MIN_W;
      var x0 = PADX, x1 = width - PADX, span = x1 - x0, dayW = span / 7;

      // group by week (a row = one week, Monday to Sunday), keep the order
      var weeks = [], byWeek = {};
      items.forEach(function (it) {
        var w = byWeek[it.week];
        if (!w) { w = byWeek[it.week] = { start: it.week, points: [] }; weeks.push(w); }
        w.points.push(it);
      });

      // lanes: points whose flags would overlap go to a higher lane
      weeks.forEach(function (w) {
        var laneEnd = [];
        w.points.forEach(function (it) {
          it.x = x0 + (it.ms - w.start) / WEEK * span;
          var half = FLAG_W / 2, k = 0;
          while (k < laneEnd.length && laneEnd[k] > it.x - half) k++;
          laneEnd[k] = it.x + half;
          it.lane = k;
        });
        w.lanes = laneEnd.length;
      });

      var svg = svgEl("svg", { width: width, role: "img" });
      svg.appendChild(svgEl("title", {}, "更新のタイムライン（1 行が 1 週間、月曜から日曜、JST。上が古く、下が新しい）"));
      var y = 8, prev = null;
      weeks.forEach(function (w) {
        if (prev !== null) {
          var gap = Math.round((w.start - prev) / WEEK) - 1;
          if (gap > 0) {                                  // weeks without an update are not drawn one by one
            svg.appendChild(svgEl("line", { x1: x0, y1: y + 10, x2: x1, y2: y + 10, "class": "gtl-gap-line" }));
            svg.appendChild(svgEl("text", { x: width / 2, y: y + 14, "class": "gtl-gap" }, "更新のない週が " + gap + " 週続きました"));
            y += 28;
          }
        }
        prev = w.start;
        var axisY = y + HEAD + w.lanes * FLAG_H + 14;
        var startD = new Date(w.start), endD = new Date(w.start + 6 * DAY);
        svg.appendChild(svgEl("text", { x: x0, y: y + 10, "class": "gtl-week" },
          startD.getUTCFullYear() + "/" + pad2(startD.getUTCMonth() + 1) + "/" + pad2(startD.getUTCDate()) + " (Mon) 〜 " +
          endD.getUTCFullYear() + "/" + pad2(endD.getUTCMonth() + 1) + "/" + pad2(endD.getUTCDate()) + " (Sun)"));
        for (var i = 0; i < 7; i++) {
          var dd = new Date(w.start + i * DAY), gx = x0 + i * dayW;
          svg.appendChild(svgEl("line", { x1: gx, y1: y + 16, x2: gx, y2: axisY + 4, "class": "gtl-grid" }));
          svg.appendChild(svgEl("text", { x: gx + 4, y: y + 28, "class": "gtl-dayname" }, DOW[dd.getUTCDay()] + " " + pad2(dd.getUTCMonth() + 1) + "/" + pad2(dd.getUTCDate())));
        }
        svg.appendChild(svgEl("line", { x1: x1, y1: y + 16, x2: x1, y2: axisY + 4, "class": "gtl-grid" }));
        svg.appendChild(svgEl("line", { x1: x0, y1: axisY, x2: x1, y2: axisY, "class": "gtl-line" }));

        w.points.forEach(function (it) {
          var flagBottom = axisY - 14 - it.lane * FLAG_H;           // the three text lines sit above the wedge
          var tx = Math.min(Math.max(it.x, x0 + FLAG_W / 2), x1 - FLAG_W / 2);
          var a = svgEl("a", { href: "#" + it.id, "class": "gtl-node" + (it.initial ? " gtl-initial" : "") });
          a.appendChild(svgEl("title", {}, it.day + " " + it.time + " JST  " + it.hash + (it.initial ? "  記録を開始" : "") + (it.text ? "\n" + it.text : "")));
          if (it.lane > 0) a.appendChild(svgEl("line", { x1: it.x, y1: flagBottom + 2, x2: it.x, y2: axisY - 12, "class": "gtl-stem" }));
          a.appendChild(svgEl("path", { d: "M " + (it.x - 6) + " " + (axisY - 12) + " L " + (it.x + 6) + " " + (axisY - 12) + " L " + it.x + " " + axisY + " Z", "class": "gtl-wedge" }));
          a.appendChild(svgEl("text", { x: tx, y: flagBottom - 27, "class": "gtl-hash" }, it.hash));
          a.appendChild(svgEl("text", { x: tx, y: flagBottom - 15, "class": "gtl-day" }, it.day));
          a.appendChild(svgEl("text", { x: tx, y: flagBottom - 4, "class": "gtl-day" }, it.time + " JST"));
          a.addEventListener("click", function () {
            var card = document.getElementById(it.id);
            if (!card) return;
            card.classList.add("gtl-flash");
            window.setTimeout(function () { card.classList.remove("gtl-flash"); }, 1800);
          });
          svg.appendChild(a);
        });
        y = axisY + 22;
      });
      svg.setAttribute("height", y);
      svg.setAttribute("viewBox", "0 0 " + width + " " + y);
      var wrap = $(".gtl-body", host);
      wrap.textContent = "";
      wrap.appendChild(svg);
    }

    host.textContent = "";
    var head = document.createElement("h3");
    head.className = "section-title";
    head.textContent = "更新タイムライン";
    var note = document.createElement("p");
    note.className = "muted gtl-note";
    var more = document.createElement("button");
    more.type = "button";
    more.className = "pill-btn gtl-more";
    function setNote() {
      note.textContent = "1行が 1週間（月曜から日曜、JST）。" +
        "更新のない週は省いています。" +
        (items.length < all.length ? "（新しい " + items.length + " 件を表示中。全部で " + all.length + " 件）" : "（全 " + all.length + " 件）");
      more.textContent = items.length < all.length ? "すべて表示（" + all.length + " 件）" : "新しい " + GTL_FIRST + " 件だけ表示";
      more.hidden = all.length <= GTL_FIRST;
    }
    more.addEventListener("click", function () {
      showAll = !showAll;
      items = showAll ? all : all.slice(all.length - GTL_FIRST);
      setNote();
      draw();
    });
    var body = document.createElement("div");
    body.className = "gtl-body";
    setNote();
    host.appendChild(head);
    host.appendChild(note);
    host.appendChild(more);
    host.appendChild(body);
    host.hidden = false;
    draw();
    window.addEventListener("resize", function () {
      window.clearTimeout(TIMER);
      TIMER = window.setTimeout(function () { if (Math.abs((host.clientWidth || MIN_W) - lastWidth) > 8) draw(); }, 150);
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
    initTimeline();
    showSharedBanner();
    apply();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();
