"use strict";
// The settings / overview screen. It talks to the Rust side only through the commands below (window.__TAURI__).
// Everything that comes from the network (site names, summaries) goes in through textContent: nothing is parsed as HTML.
(function () {
  const invoke = window.__TAURI__.core.invoke;
  const $ = (id) => document.getElementById(id);
  let sites = [], follow = new Set();

  const say = (t) => { $("msg").textContent = t; };
  const when = (ms) => (ms ? new Date(ms).toLocaleString() : "まだ確認していません");

  function statusLine(v) {
    $("status").textContent = `フォロー中 ${v.follow.length} サイト ・ 最終確認 ${when(v.lastCheck)}`
      + (v.follow.length ? ` ・ 次回 ${new Date(v.nextCheck).toLocaleString()}` : "")
      + (v.lastError ? ` ・ 注意: ${v.lastError}` : "");
    $("scheduleNote").textContent = v.scheduleText;
  }

  function renderRecent(list) {
    const ul = $("recent");
    ul.textContent = "";
    $("recentEmpty").hidden = list.length > 0;
    for (const r of list) {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = "#";
      a.textContent = `${r.name}  ${r.time}`;
      a.addEventListener("click", (e) => { e.preventDefault(); invoke("open_external", { url: r.count > 1 ? r.historyUrl : r.diffUrl }); });
      const small = document.createElement("small");
      small.textContent = "  " + r.text;
      li.append(a, small);
      ul.append(li);
    }
  }

  function renderSites() {
    const q = $("filter").value.trim().toLowerCase();
    const only = $("only").checked;
    const list = $("sites");
    list.textContent = "";
    let shown = 0;
    for (const s of sites) {
      if (only && !follow.has(s.slug)) continue;
      if (q && !(s.name + " " + s.url + " " + s.slug).toLowerCase().includes(q)) continue;
      shown++;
      const li = document.createElement("li");
      const label = document.createElement("label");
      const cb = document.createElement("input");
      cb.type = "checkbox"; cb.checked = follow.has(s.slug);
      cb.addEventListener("change", async () => {
        cb.checked ? follow.add(s.slug) : follow.delete(s.slug);
        await invoke("set_follow", { slugs: [...follow] });
        $("count").textContent = `${follow.size} / ${sites.length} 件をフォロー中`;
      });
      const name = document.createElement("span"); name.textContent = s.name;
      const host = document.createElement("small");
      try { host.textContent = new URL(s.url).hostname.replace(/^www\./, ""); } catch { host.textContent = s.slug; }
      label.append(cb, name, host); li.append(label); list.append(li);
    }
    $("count").textContent = `${follow.size} / ${sites.length} 件をフォロー中（${shown} 件を表示）`;
  }

  async function load() {
    const v = await invoke("get_state");
    sites = v.sites; follow = new Set(v.follow);
    $("baseUrl").value = v.baseUrl; $("interval").value = v.intervalMin; $("login").checked = v.openAtLogin;
    statusLine(v); renderRecent(v.recent); renderSites();
    if (!sites.length) say("サイトの一覧を取得できませんでした。ダッシュボードの URL とネットワークを、確認してください。");
  }

  $("filter").addEventListener("input", renderSites);
  $("only").addEventListener("change", renderSites);
  $("importBtn").addEventListener("click", async () => {
    const r = await invoke("import_follow", { text: $("import").value });
    follow = new Set(r.follow); $("import").value = "";
    say(r.added ? `${r.added} 件を取り込みました。` : "取り込めるサイトが、見つかりませんでした。"); renderSites();
  });
  $("saveBtn").addEventListener("click", async () => {
    try {
      await invoke("set_options", { baseUrl: $("baseUrl").value, intervalMin: Number($("interval").value) || 15, openAtLogin: $("login").checked });
      say("保存しました。"); await load();
    } catch (e) { say("保存できませんでした: " + e); }
  });
  $("checkBtn").addEventListener("click", async () => {
    say("確認しています…");
    await invoke("check_now");
    setTimeout(async () => { await load(); say("確認しました。"); }, 4000);
  });
  $("testBtn").addEventListener("click", () => invoke("test_notification"));
  // desktop only: the start-at-login switch has no meaning on a phone
  if (/android|iphone|ipad/i.test(navigator.userAgent)) { $("login").hidden = true; $("loginLabel").hidden = true; }
  load();
})();
