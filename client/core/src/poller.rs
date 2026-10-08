//! One check of all followed sites. No globals and no OS code: the fetcher and the notification callback are passed in.

use crate::feed::{parse_atom, parse_sites, Entry};
use crate::state::{Recent, State, RECENT_LIMIT, SEEN_LIMIT};
use chrono::{DateTime, FixedOffset};
use std::time::Duration;

/// More new entries than this at once -> one summary notification instead of many.
pub const BURST: usize = 3;
const SITES_MAX_AGE_MS: i64 = 24 * 3600 * 1000;

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

/// The site names (search.json) are refreshed when missing or older than a day.
pub fn refresh_sites(state: &mut State, f: &dyn Fetcher, now_ms: i64) -> Result<bool, String> {
    if !state.sites.is_empty() && now_ms - state.sites_at < SITES_MAX_AGE_MS {
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

/// Checks every followed site once and calls `notify` for each new update.
///  - a site seen for the first time is only remembered (no flood of old updates),
///  - ids already known are never announced again; a 304 (ETag) costs almost nothing,
///  - one failing site does not stop the others.
/// `state` is changed in place; the caller saves it.
pub fn poll_once(state: &mut State, f: &dyn Fetcher, notify: &mut dyn FnMut(&Notice), now_ms: i64) -> PollResult {
    let mut result = PollResult::default();
    if let Err(e) = refresh_sites(state, f, now_ms) {
        result.errors.push(("(sites)".into(), e));
    }
    let follow = state.follow.clone();
    for slug in &follow {
        let name = state.sites.iter().find(|s| &s.slug == slug).map(|s| s.name.clone()).unwrap_or_else(|| slug.clone());
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
        let Some(seen) = state.seen.get(slug).cloned() else {
            state.seen.insert(slug.clone(), entries.iter().take(SEEN_LIMIT).map(|e| e.id.clone()).collect());
            result.first_seen.push(slug.clone());
            continue;
        };
        let fresh: Vec<&Entry> = entries.iter().filter(|e| !seen.contains(&e.id)).rev().collect();     // oldest first
        if fresh.is_empty() {
            continue;
        }
        let base = state.base_url.clone();
        let mut notices: Vec<Notice> = Vec::new();
        if fresh.len() > BURST {
            let last = fresh[fresh.len() - 1];
            notices.push(Notice {
                slug: slug.clone(),
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
                    slug: slug.clone(),
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
            state.recent.insert(0, Recent {
                slug: n.slug.clone(),
                name: n.title.clone(),
                time: n.body.lines().next().unwrap_or("").to_string(),
                text: n.body.lines().skip(1).collect::<Vec<_>>().join(" "),
                count: n.count,
                diff_url: n.diff_url.clone(),
                history_url: n.history_url.clone(),
                at: now_ms,
            });
        }
        state.recent.truncate(RECENT_LIMIT);
        let mut all = seen;
        all.extend(fresh.iter().map(|e| e.id.clone()));
        let skip = all.len().saturating_sub(SEEN_LIMIT);
        state.seen.insert(slug.clone(), all.into_iter().skip(skip).collect());
    }
    state.last_check = now_ms;
    state.last_error = result.errors.iter().map(|(s, m)| format!("{s}: {m}")).collect::<Vec<_>>().join(" / ").chars().take(300).collect();
    result
}
