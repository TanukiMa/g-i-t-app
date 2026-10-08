//! The client's own small database: one JSON file in the app's data folder. Nothing is sent anywhere.

use crate::feed::Site;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::Path;

pub const DEFAULT_BASE_URL: &str = "https://tanukima.github.io/g-i-t-data/";
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
    std::fs::read_to_string(path).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default()
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
