//! The client's own small database: one JSON file in the app's data folder. Nothing is sent anywhere.

use crate::feed::Site;
use crate::schedule::Schedule;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::Path;

pub const DEFAULT_BASE_URL: &str = "https://giiit.goudge.org/";
/// The address before the move to its own domain: it still forwards and still serves the feeds, but a state that has it
/// as the dashboard address is moved to DEFAULT_BASE_URL when it is loaded.
pub const OLD_DEFAULT_BASE_URL: &str = "https://tanukima.github.io/g-i-t-data/";
pub const SEEN_LIMIT: usize = 300;
pub const RECENT_LIMIT: usize = 50;

/// A notification that was shown (kept so that it can be opened later: desktop notifications cannot be clicked on every OS).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct Recent {
    pub slug: String,
    pub name: String,
    /// "2026-10-07 12:30 JST"
    pub time: String,
    pub text: String,
    pub count: usize,
    pub diff_url: String,
    pub history_url: String,
    /// Unix milliseconds when it was found.
    pub at: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct State {
    pub v: u32,
    pub base_url: String,
    pub interval_min: u32,
    /// Slugs of the followed sites.
    pub follow: Vec<String>,
    /// slug -> ids of the entries already known (the newest `SEEN_LIMIT`).
    pub seen: HashMap<String, Vec<String>>,
    /// slug -> ETag of the last feed.
    pub etags: HashMap<String, String>,
    pub sites: Vec<Site>,
    pub sites_at: i64,
    pub last_check: i64,
    pub last_error: String,
    pub recent: Vec<Recent>,
    /// True while the site publishes feeds/status.json: the app then looks only after the runs (see schedule.rs).
    /// False (an older site, or not read yet): every `interval_min` minutes, all followed feeds, as before.
    pub status_mode: bool,
    pub status_etag: String,
    /// The last status.json: slug -> newest update (7-character hash).
    pub status_sites: HashMap<String, String>,
    /// slug -> the newest update whose feed has been read.
    pub heads: HashMap<String, String>,
    pub schedule: Schedule,
    /// `generated` of the last status.json (Unix ms): the time the last run finished.
    pub generated_ms: i64,
}

impl Default for State {
    fn default() -> Self {
        State {
            v: 1,
            base_url: DEFAULT_BASE_URL.to_string(),
            interval_min: 15,
            follow: vec![],
            seen: HashMap::new(),
            etags: HashMap::new(),
            sites: vec![],
            sites_at: 0,
            last_check: 0,
            last_error: String::new(),
            recent: vec![],
            status_mode: false,
            status_etag: String::new(),
            status_sites: HashMap::new(),
            heads: HashMap::new(),
            schedule: Schedule::default(),
            generated_ms: 0,
        }
    }
}

impl State {
    /// Copies back what a check changed, without touching what the user changed meanwhile (follow list, options).
    pub fn merge_poll(&mut self, polled: State) {
        self.seen = polled.seen;
        self.etags = polled.etags;
        self.sites = polled.sites;
        self.sites_at = polled.sites_at;
        self.last_check = polled.last_check;
        self.last_error = polled.last_error;
        self.recent = polled.recent;
        self.status_mode = polled.status_mode;
        self.status_etag = polled.status_etag;
        self.status_sites = polled.status_sites;
        self.heads = polled.heads;
        self.schedule = polled.schedule;
        self.generated_ms = polled.generated_ms;
    }

    /// Forget everything that belongs to the site that was read so far (another dashboard URL).
    pub fn forget_site_data(&mut self) {
        self.sites.clear();
        self.sites_at = 0;
        self.seen.clear();
        self.etags.clear();
        self.recent.clear();
        self.status_mode = false;
        self.status_etag.clear();
        self.status_sites.clear();
        self.heads.clear();
        self.generated_ms = 0;
    }
}

pub fn normalize_base(url: &str) -> Result<String, String> {
    let mut u = url.trim().to_string();
    let lower = u.to_lowercase();
    if !(lower.starts_with("http://") || lower.starts_with("https://")) {
        return Err("URL は http:// か https:// で始めてください".into());
    }
    if !u.ends_with('/') {
        u.push('/');
    }
    Ok(u)
}

pub fn load(path: &Path) -> State {
    let mut state: State = std::fs::read_to_string(path).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default();
    state.migrate();
    state
}

impl State {
    /// Brings a state written by an older version up to date. Safe to run on every load.
    ///  - what is remembered about an update is "<slug>/<commit>" (see Entry::key); older states hold the whole entry id,
    ///    which carries the host name, so the move to the new domain would make every entry look new;
    ///  - the old default address becomes the new one (the ETags belong to the old address and are dropped).
    pub fn migrate(&mut self) {
        for ids in self.seen.values_mut() {
            for id in ids.iter_mut() {
                *id = crate::feed::seen_key(id);
            }
        }
        if self.base_url == OLD_DEFAULT_BASE_URL {
            self.base_url = DEFAULT_BASE_URL.to_string();
            self.etags.clear();
            self.status_etag.clear();
        }
    }
}

/// Atomic: a crash never leaves a half-written file.
pub fn save(path: &Path, state: &State) -> std::io::Result<()> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let tmp = path.with_extension("json.tmp");
    std::fs::write(&tmp, serde_json::to_string_pretty(state).map_err(std::io::Error::other)?)?;
    std::fs::rename(&tmp, path)
}
