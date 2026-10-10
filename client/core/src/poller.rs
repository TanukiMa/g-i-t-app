//! One check of all followed sites. No globals and no OS code: the fetcher and the notification callback are passed in.

use crate::feed::{parse_atom, parse_sites, parse_status, Entry};
use crate::schedule::Schedule;
use crate::state::{Recent, State, RECENT_LIMIT, SEEN_LIMIT};
use chrono::{DateTime, FixedOffset};
use std::time::Duration;

/// More new entries than this at once -> one summary notification instead of many.
pub const BURST: usize = 3;
const SITES_MAX_AGE_MS: i64 = 7 * 24 * 3600 * 1000;

pub struct FetchResult {
    pub status: u16,
    pub text: String,
    pub etag: String,
}

pub trait Fetcher {
    fn get(&self, url: &str, etag: Option<&str>) -> Result<FetchResult, String>;
}

/// What the platform shows. `diff_url` / `history_url` are built from the configured base URL, not from the feed's host.
#[derive(Debug, Clone)]
pub struct Notice {
    pub slug: String,
    pub title: String,
    pub body: String,
    pub count: usize,
    pub diff_url: String,
    pub history_url: String,
}

#[derive(Debug, Default)]
pub struct PollResult {
    pub checked: usize,
    pub notified: usize,
    pub errors: Vec<(String, String)>,
    pub first_seen: Vec<String>,
    /// True when status.json was read (false: every feed was read, the old way).
    pub via_status: bool,
    /// Followed sites whose newest update had changed (their feeds were read).
    pub status_changed: usize,
}

/// The real network: ureq, a time limit, ETag.
pub struct HttpFetcher {
    agent: ureq::Agent,
}

impl HttpFetcher {
    pub fn new() -> Self {
        HttpFetcher { agent: ureq::AgentBuilder::new().timeout(Duration::from_secs(20)).user_agent("g-i-t-client").build() }
    }
}

impl Default for HttpFetcher {
    fn default() -> Self {
        Self::new()
    }
}

impl Fetcher for HttpFetcher {
    fn get(&self, url: &str, etag: Option<&str>) -> Result<FetchResult, String> {
        let mut req = self.agent.get(url);
        if let Some(e) = etag {
            req = req.set("If-None-Match", e);
        }
        match req.call() {
            Ok(res) => {
                let etag = res.header("etag").unwrap_or("").to_string();
                let status = res.status();
                Ok(FetchResult { status, text: res.into_string().map_err(|e| e.to_string())?, etag })
            }
            Err(ureq::Error::Status(code, res)) => Ok(FetchResult { status: code, text: String::new(), etag: res.header("etag").unwrap_or("").to_string() }),
            Err(e) => Err(e.to_string()),
        }
    }
}

/// "2026-10-07T03:30:00Z" -> "2026-10-07 12:30 JST"
pub fn jst(iso: &str) -> String {
    match DateTime::parse_from_rfc3339(iso) {
        Ok(d) => d.with_timezone(&FixedOffset::east_opt(9 * 3600).unwrap()).format("%Y-%m-%d %H:%M JST").to_string(),
        Err(_) => String::new(),
    }
}

fn diff_url(base: &str, e: &Entry) -> String {
    format!("{base}sites/{}/diff_{}.html", e.slug, &e.hash[..7])
}

fn history_url(base: &str, slug: &str) -> String {
    format!("{base}sites/{slug}/history.html")
}

/// The site names (search.json) are refreshed when missing or older than a week, or when a site is not in them
/// (`unknown`: a followed site, or a site that status.json lists, that the names do not contain yet).
pub fn refresh_sites(state: &mut State, f: &dyn Fetcher, now_ms: i64) -> Result<bool, String> {
    refresh_sites_if(state, f, now_ms, false)
}

fn refresh_sites_if(state: &mut State, f: &dyn Fetcher, now_ms: i64, unknown: bool) -> Result<bool, String> {
    if !unknown && !state.sites.is_empty() && now_ms - state.sites_at < SITES_MAX_AGE_MS {
        return Ok(false);
    }
    let r = f.get(&format!("{}search.json", state.base_url), None)?;
    if r.status != 200 {
        return Err(format!("search.json: HTTP {}", r.status));
    }
    state.sites = parse_sites(&r.text)?;
    state.sites_at = now_ms;
    Ok(true)
}

/// What to do with the entries of one site's feed: remember them (first time) or announce the new ones.
///  - a site seen for the first time is only remembered (no flood of old updates),
///  - ids already known are never announced again,
///  - more than BURST new ones at once -> one summary notification.
fn handle_entries(state: &mut State, slug: &str, entries: Vec<Entry>, notify: &mut dyn FnMut(&Notice), now_ms: i64, result: &mut PollResult) {
    let name = state.sites.iter().find(|s| s.slug == slug).map(|s| s.name.clone()).unwrap_or_else(|| slug.to_string());
    let Some(seen) = state.seen.get(slug).cloned() else {
        state.seen.insert(slug.to_string(), entries.iter().take(SEEN_LIMIT).map(|e| e.key()).collect());
        result.first_seen.push(slug.to_string());
        return;
    };
    let fresh: Vec<&Entry> = entries.iter().filter(|e| !seen.contains(&e.key())).rev().collect(); // oldest first
    if fresh.is_empty() {
        return;
    }
    let base = state.base_url.clone();
    let mut notices: Vec<Notice> = Vec::new();
    if fresh.len() > BURST {
        let last = fresh[fresh.len() - 1];
        notices.push(Notice {
            slug: slug.to_string(),
            title: name.clone(),
            body: format!("{} 件の更新を検知しました（最新: {}）", fresh.len(), jst(&last.updated)),
            count: fresh.len(),
            diff_url: diff_url(&base, last),
            history_url: history_url(&base, slug),
        });
    } else {
        for e in &fresh {
            let headline = e.title.strip_prefix(name.as_str()).map(|t| t.trim_start_matches([':', '：', ' ']).to_string()).unwrap_or_else(|| e.title.clone());
            let text = if headline.is_empty() { e.text.clone() } else { headline };
            notices.push(Notice {
                slug: slug.to_string(),
                title: name.clone(),
                body: format!("{}\n{}", jst(&e.updated), text),
                count: 1,
                diff_url: diff_url(&base, e),
                history_url: history_url(&base, slug),
            });
        }
    }
    for n in &notices {
        notify(n);
        result.notified += 1;
        state.recent.insert(
            0,
            Recent {
                slug: n.slug.clone(),
                name: n.title.clone(),
                time: n.body.lines().next().unwrap_or("").to_string(),
                text: n.body.lines().skip(1).collect::<Vec<_>>().join(" "),
                count: n.count,
                diff_url: n.diff_url.clone(),
                history_url: n.history_url.clone(),
                at: now_ms,
            },
        );
    }
    state.recent.truncate(RECENT_LIMIT);
    let mut all = seen;
    all.extend(fresh.iter().map(|e| e.key()));
    let skip = all.len().saturating_sub(SEEN_LIMIT);
    state.seen.insert(slug.to_string(), all.into_iter().skip(skip).collect());
}

fn finish(state: &mut State, result: &PollResult, now_ms: i64) {
    state.last_check = now_ms;
    state.last_error = result.errors.iter().map(|(s, m)| format!("{s}: {m}")).collect::<Vec<_>>().join(" / ").chars().take(300).collect();
}

/// The old way: every followed site's feed, one request each (a 304 costs almost nothing). Used when the site does not
/// publish feeds/status.json. One failing site does not stop the others. `state` is changed in place; the caller saves it.
pub fn poll_once(state: &mut State, f: &dyn Fetcher, notify: &mut dyn FnMut(&Notice), now_ms: i64) -> PollResult {
    let mut result = PollResult::default();
    if let Err(e) = refresh_sites(state, f, now_ms) {
        result.errors.push(("(sites)".into(), e));
    }
    let follow = state.follow.clone();
    for slug in &follow {
        let r = match f.get(&format!("{}feeds/{slug}.xml", state.base_url), state.etags.get(slug).map(String::as_str)) {
            Ok(r) => r,
            Err(e) => {
                result.errors.push((slug.clone(), e));
                continue;
            }
        };
        result.checked += 1;
        match r.status {
            304 => continue,
            404 => {
                result.errors.push((slug.clone(), "フィードがありません（サイトが、まだありません）".into()));
                continue;
            }
            200 => {}
            code => {
                result.errors.push((slug.clone(), format!("HTTP {code}")));
                continue;
            }
        }
        let entries: Vec<Entry> = match parse_atom(&r.text) {
            Ok(v) => v.into_iter().filter(|e| &e.slug == slug).collect(),
            Err(e) => {
                result.errors.push((slug.clone(), e));
                continue;
            }
        };
        if !r.etag.is_empty() {
            state.etags.insert(slug.clone(), r.etag.clone());
        }
        handle_entries(state, slug, entries, notify, now_ms, &mut result);
    }
    finish(state, &result, now_ms);
    result
}

/// The way after the pipeline has been told to publish feeds/status.json: ONE small file says which sites have a new
/// newest update, and only the feeds of followed sites that changed are read (at an address with `?h=<hash>`, so that no
/// cache can hand out the old feed). When the site has no status.json (404) it falls back to `poll_once`.
/// A network error ends the check without touching anything (the next look is made by the schedule).
pub fn poll_scheduled(state: &mut State, f: &dyn Fetcher, notify: &mut dyn FnMut(&Notice), now_ms: i64) -> PollResult {
    let mut result = PollResult::default();
    let url = format!("{}feeds/status.json", state.base_url);
    let etag = (!state.status_etag.is_empty() && state.status_mode).then(|| state.status_etag.clone());
    let r = match f.get(&url, etag.as_deref()) {
        Ok(r) => r,
        Err(e) => {
            result.errors.push(("(status)".into(), e));
            finish_error_only(state, &result);
            return result;
        }
    };
    result.checked += 1;
    match r.status {
        200 => match parse_status(&r.text) {
            Ok(s) => {
                state.schedule = Schedule { runs: s.runs, delay_min: s.delay_min, window_min: s.window_min, offset_min: s.offset_min, ..state.schedule.clone() };
                state.generated_ms = s.generated_ms;
                state.status_sites = s.sites;
                state.status_etag = r.etag;
                state.status_mode = true;
            }
            Err(e) => {
                result.errors.push(("(status)".into(), e));
                finish_error_only(state, &result);
                return result;
            }
        },
        304 if state.status_mode => {}
        404 => {
            state.status_mode = false; // an older site: look at every feed, on the interval
            return poll_once(state, f, notify, now_ms);
        }
        code => {
            result.errors.push(("(status)".into(), format!("HTTP {code}")));
            finish_error_only(state, &result);
            return result;
        }
    }
    result.via_status = true;

    // Names of the sites: also when a site appeared that they do not know yet.
    let unknown = state.status_sites.keys().any(|slug| !state.sites.iter().any(|s| &s.slug == slug));
    if let Err(e) = refresh_sites_if(state, f, now_ms, unknown) {
        result.errors.push(("(sites)".into(), e));
    }

    let follow = state.follow.clone();
    for slug in &follow {
        let Some(head) = state.status_sites.get(slug).cloned() else {
            // no real update yet: remember that the site is known, so that its first update is announced
            if !state.seen.contains_key(slug) {
                state.seen.insert(slug.clone(), vec![]);
                result.first_seen.push(slug.clone());
            }
            continue;
        };
        if state.heads.get(slug) == Some(&head) {
            continue; // nothing new: no request at all
        }
        let r = match f.get(&format!("{}feeds/{slug}.xml?h={head}", state.base_url), None) {
            Ok(r) => r,
            Err(e) => {
                result.errors.push((slug.clone(), e));
                continue;
            }
        };
        result.checked += 1;
        if r.status != 200 {
            result.errors.push((slug.clone(), format!("HTTP {}", r.status)));
            continue;
        }
        let entries: Vec<Entry> = match parse_atom(&r.text) {
            Ok(v) => v.into_iter().filter(|e| &e.slug == slug).collect(),
            Err(e) => {
                result.errors.push((slug.clone(), e));
                continue;
            }
        };
        if !entries.iter().any(|e| e.hash.starts_with(&head)) {
            result.errors.push((slug.clone(), "フィードがまだ古い（次の確認でもう一度読みます）".into()));
            continue; // the head is not remembered, so the next look reads the feed again
        }
        handle_entries(state, slug, entries, notify, now_ms, &mut result);
        state.heads.insert(slug.clone(), head);
        result.status_changed += 1;
    }
    finish(state, &result, now_ms);
    result
}

/// A failed look at status.json: only the message is kept (`last_check` stays, so that "last check" means a check that worked).
fn finish_error_only(state: &mut State, result: &PollResult) {
    state.last_error = result.errors.iter().map(|(s, m)| format!("{s}: {m}")).collect::<Vec<_>>().join(" / ").chars().take(300).collect();
}

/// When the next look is due (Unix ms): after the runs (see schedule.rs) while the site publishes status.json,
/// otherwise `interval_min` after the last check. Never in the past.
pub fn next_check_ms(state: &State, now_ms: i64) -> i64 {
    if state.last_check == 0 {
        return now_ms;
    }
    let interval_ms = state.interval_min.clamp(5, 240) as i64 * 60_000;
    let at = if !state.status_mode {
        state.last_check + interval_ms
    } else if state.last_check < state.schedule.latest_window_start(now_ms) {
        now_ms // a window began after the last look (the app was closed or the computer asleep): catch up now
    } else {
        state.schedule.next_check(now_ms, state.generated_ms)
    };
    at.max(now_ms)
}
