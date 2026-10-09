//! When to look for news. The pipeline runs at fixed times (published as `runs` in `feeds/status.json`), so a client
//! looks only in a short window after each run instead of every few minutes all day.
//!
//! Times are minutes after midnight in the zone of the site (JST, `offset_min` = 540). Pure functions of the clock
//! that is passed in: no OS code, easy to test.

use serde::{Deserialize, Serialize};

const MIN_MS: i64 = 60_000;
const DAY_MS: i64 = 86_400_000;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub struct Schedule {
    /// Start of each run, minutes after midnight (site time), ascending.
    pub runs: Vec<u32>,
    /// Minutes after the start of a run at which the news can be there.
    pub delay_min: u32,
    /// How long to look after that.
    pub window_min: u32,
    /// Minutes between two looks inside the window.
    pub step_min: u32,
    /// Zone of the site in minutes east of UTC.
    pub offset_min: i32,
}

impl Default for Schedule {
    fn default() -> Self {
        // 7:30, 12:30, 16:30 JST: what the site publishes; used until status.json has been read once
        Schedule { runs: vec![7 * 60 + 30, 12 * 60 + 30, 16 * 60 + 30], delay_min: 8, window_min: 25, step_min: 3, offset_min: 540 }
    }
}

impl Schedule {
    /// "7:30・12:30・16:30"
    pub fn runs_text(&self) -> String {
        self.runs.iter().map(|m| format!("{}:{:02}", m / 60, m % 60)).collect::<Vec<_>>().join("・")
    }

    /// Start (Unix ms) of every run of the day before, the day of and the day after `now_ms`, ascending.
    fn run_starts(&self, now_ms: i64) -> Vec<i64> {
        let off = self.offset_min as i64 * MIN_MS;
        let day0 = (now_ms + off).div_euclid(DAY_MS) * DAY_MS - off;
        let mut out = Vec::with_capacity(self.runs.len() * 3);
        for d in -1..=1 {
            for m in &self.runs {
                out.push(day0 + d * DAY_MS + *m as i64 * MIN_MS);
            }
        }
        out.sort_unstable();
        out
    }

    fn window(&self, run_start: i64) -> (i64, i64) {
        let from = run_start + self.delay_min as i64 * MIN_MS;
        (from, from + self.window_min as i64 * MIN_MS)
    }

    /// The run whose window contains `now_ms` (its start), if any.
    pub fn current_run(&self, now_ms: i64) -> Option<i64> {
        self.run_starts(now_ms).into_iter().find(|r| {
            let (from, to) = self.window(*r);
            from <= now_ms && now_ms <= to
        })
    }

    /// The start of the latest window that has begun at or before `now_ms`.
    pub fn latest_window_start(&self, now_ms: i64) -> i64 {
        self.run_starts(now_ms).into_iter().map(|r| self.window(r).0).filter(|from| *from <= now_ms).max().unwrap_or(now_ms)
    }

    /// The start of the first window that begins after `now_ms`.
    pub fn next_window_start(&self, now_ms: i64) -> i64 {
        self.run_starts(now_ms).into_iter().map(|r| self.window(r).0).find(|from| *from > now_ms).unwrap_or(now_ms + DAY_MS)
    }

    /// When to look next. Inside a window: again after `step_min`, until the status says that this run has finished
    /// (`generated_ms` is not before the start of the run). Otherwise: at the start of the next window.
    pub fn next_check(&self, now_ms: i64, generated_ms: i64) -> i64 {
        if let Some(run) = self.current_run(now_ms) {
            if generated_ms < run {
                return now_ms + self.step_min.max(1) as i64 * MIN_MS;
            }
        }
        self.next_window_start(now_ms)
    }
}
