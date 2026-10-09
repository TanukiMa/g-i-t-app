//! status.json, the schedule and the scheduled poll (poll_scheduled), with a fake network.

use git_core::feed::{parse_status, Site};
use git_core::poller::{next_check_ms, poll_scheduled, FetchResult, Fetcher, Notice, PollResult};
use git_core::schedule::Schedule;
use git_core::state::State;
use std::cell::RefCell;
use std::collections::HashMap;

const MIN: i64 = 60_000;

/// "2026-10-09 12:41" JST -> Unix ms
fn jst(s: &str) -> i64 {
    let iso = format!("{}T{}:00+09:00", &s[..10], &s[11..16]);
    chrono::DateTime::parse_from_rfc3339(&iso).unwrap().timestamp_millis()
}

fn status_json(generated: &str, sites: &[(&str, &str)]) -> String {
    let sites: Vec<String> = sites.iter().map(|(k, v)| format!("\"{k}\":\"{v}\"")).collect();
    format!(
        r#"{{"v":1,"generated":"{generated}","tz":"Asia/Tokyo","utcOffsetMin":540,"runs":["08:30","12:30","16:30","22:00"],"delayMin":8,"windowMin":25,"sites":{{{}}}}}"#,
        sites.join(",")
    )
}

// ---- status.json ----
#[test]
fn status_json_is_read() {
    let s = parse_status(&status_json("2026-10-09T03:41:00Z", &[("a", "abcdef1"), ("b-2", "1234567")])).unwrap();
    assert_eq!(s.runs, [510, 750, 990, 1320]);
    assert_eq!((s.delay_min, s.window_min, s.offset_min), (8, 25, 540));
    assert_eq!(s.sites.get("a").map(String::as_str), Some("abcdef1"));
    assert_eq!(s.generated_ms, jst("2026-10-09 12:41"));
}

#[test]
fn status_json_rejects_what_it_cannot_use() {
    assert!(parse_status("not json").is_err());
    assert!(parse_status(r#"{"generated":"2026-10-09T03:41:00Z","runs":[],"sites":{}}"#).is_err()); // no run times
    assert!(parse_status(r#"{"generated":"yesterday","runs":["08:30"],"sites":{}}"#).is_err());
    assert!(parse_status(r#"{"generated":"2026-10-09T03:41:00Z","runs":["08:30"]}"#).is_err()); // no sites
    // bad slugs and bad times are dropped, not fatal
    let s = parse_status(r#"{"generated":"2026-10-09T03:41:00Z","runs":["8:30x","25:00","09:15"],"sites":{"Ok":"1","../x":"2","fine":"3"}}"#).unwrap();
    assert_eq!(s.runs, [555]);
    assert_eq!(s.sites.keys().cloned().collect::<Vec<_>>(), ["fine"]);
}

// ---- the schedule ----
#[test]
fn the_default_schedule_is_the_published_one() {
    let sch = Schedule::default();
    assert_eq!(sch.runs_text(), "8:30・12:30・16:30・22:00");
}

#[test]
fn a_window_opens_after_the_run_and_closes() {
    let sch = Schedule::default(); // 12:30 run -> window 12:38 .. 13:03
    assert_eq!(sch.current_run(jst("2026-10-09 12:37")), None);
    assert_eq!(sch.current_run(jst("2026-10-09 12:38")), Some(jst("2026-10-09 12:30")));
    assert_eq!(sch.current_run(jst("2026-10-09 13:03")), Some(jst("2026-10-09 12:30")));
    assert_eq!(sch.current_run(jst("2026-10-09 13:04")), None);
    assert_eq!(sch.next_window_start(jst("2026-10-09 13:10")), jst("2026-10-09 16:38"));
    assert_eq!(sch.next_window_start(jst("2026-10-09 22:50")), jst("2026-10-10 08:38")); // over midnight
    assert_eq!(sch.latest_window_start(jst("2026-10-09 07:00")), jst("2026-10-08 22:08")); // yesterday's last window
}

#[test]
fn inside_a_window_it_looks_every_few_minutes_until_the_run_is_seen_to_be_finished() {
    let sch = Schedule::default();
    let now = jst("2026-10-09 12:40");
    // the last build is from the 8:30 run: this run has not finished
    assert_eq!(sch.next_check(now, jst("2026-10-09 08:41")), now + 3 * MIN);
    // the status now says it was built at 12:41 (after the 12:30 start): done, wait for the 16:30 window
    assert_eq!(sch.next_check(now, jst("2026-10-09 12:41")), jst("2026-10-09 16:38"));
    // outside any window
    assert_eq!(sch.next_check(jst("2026-10-09 14:00"), jst("2026-10-09 12:41")), jst("2026-10-09 16:38"));
}

// ---- next_check_ms: what the checking thread asks ----
#[test]
fn the_next_look_depends_on_the_mode() {
    let mut st = State::default();
    let now = jst("2026-10-09 14:00");
    assert_eq!(next_check_ms(&st, now), now, "never checked: now");

    st.last_check = jst("2026-10-09 13:50");
    st.interval_min = 15;
    assert_eq!(next_check_ms(&st, now), jst("2026-10-09 14:05"), "no status.json: every interval");

    st.status_mode = true;
    st.generated_ms = jst("2026-10-09 12:41");
    assert_eq!(next_check_ms(&st, now), jst("2026-10-09 16:38"), "status.json: after the next run");

    // the app was closed during the 12:38 window: the first look afterwards is at once
    st.last_check = jst("2026-10-09 11:00");
    assert_eq!(next_check_ms(&st, now), now, "a window has been missed: catch up");
}

// ---- poll_scheduled ----
struct Net {
    /// url -> (status, body, etag)
    pages: RefCell<HashMap<String, (u16, String, String)>>,
    calls: RefCell<Vec<String>>,
    down: RefCell<bool>,
}

impl Net {
    fn new() -> Self {
        Net { pages: RefCell::new(HashMap::new()), calls: RefCell::new(vec![]), down: RefCell::new(false) }
    }
    fn put(&self, url: &str, status: u16, body: &str, etag: &str) {
        self.pages.borrow_mut().insert(format!("https://dash.example/{url}"), (status, body.into(), etag.into()));
    }
    fn paths(&self) -> Vec<String> {
        self.calls.borrow().iter().map(|u| u.trim_start_matches("https://dash.example/").to_string()).collect()
    }
}

impl Fetcher for Net {
    fn get(&self, url: &str, etag: Option<&str>) -> Result<FetchResult, String> {
        self.calls.borrow_mut().push(url.to_string());
        if *self.down.borrow() {
            return Err("offline".into());
        }
        let pages = self.pages.borrow();
        let Some((status, body, tag)) = pages.get(url) else { return Ok(FetchResult { status: 404, text: String::new(), etag: String::new() }) };
        if *status == 200 && !tag.is_empty() && etag == Some(tag.as_str()) {
            return Ok(FetchResult { status: 304, text: String::new(), etag: tag.clone() });
        }
        Ok(FetchResult { status: *status, text: if *status == 200 { body.clone() } else { String::new() }, etag: tag.clone() })
    }
}

fn hash(n: usize) -> String {
    format!("{n:x}").repeat(40).chars().take(40).collect()
}

fn atom(slug: &str, ns: &[usize]) -> String {
    let entries: String = ns
        .iter()
        .map(|n| {
            format!(
                r#"<entry><title>サイト: 追加 {n}</title><id>tag:x,2026:g-i-t-data/{slug}/{}</id><updated>2026-10-07T0{n}:30:00Z</updated><content type="html">&lt;ul&gt;&lt;li&gt;追加: お知らせ{n}&lt;/li&gt;&lt;/ul&gt;</content></entry>"#,
                hash(*n)
            )
        })
        .collect();
    format!(r#"<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>x</title><id>y</id><updated>2026-10-07T00:00:00Z</updated>{entries}</feed>"#)
}

fn head(n: usize) -> String {
    hash(n)[..7].to_string()
}

fn state(follow: &[&str]) -> State {
    let mut st = State::default();
    st.base_url = "https://dash.example/".into();
    st.follow = follow.iter().map(|s| s.to_string()).collect();
    st.sites = ["a", "b"].iter().map(|s| Site { slug: s.to_string(), name: "サイト".into(), url: "https://o.example/".into(), tags: vec![] }).collect();
    st.sites_at = 1_000;
    st
}

fn look(st: &mut State, net: &Net) -> (PollResult, Vec<Notice>) {
    let mut notes = vec![];
    let r = poll_scheduled(st, net, &mut |n| notes.push(n.clone()), 2_000);
    (r, notes)
}

#[test]
fn only_the_status_is_read_when_nothing_changed() {
    let mut st = state(&["a", "b"]);
    let net = Net::new();
    net.put("feeds/status.json", 200, &status_json("2026-10-09T03:41:00Z", &[("a", &head(1)), ("b", &head(1))]), "\"s1\"");
    net.put(&format!("feeds/a.xml?h={}", head(1)), 200, &atom("a", &[1]), "");
    net.put(&format!("feeds/b.xml?h={}", head(1)), 200, &atom("b", &[1]), "");

    let (r, notes) = look(&mut st, &net); // first look: the feeds are read and remembered, nobody is told
    assert!(notes.is_empty() && r.via_status);
    assert_eq!(r.first_seen.len(), 2);
    assert!(st.status_mode);
    assert_eq!(st.generated_ms, jst("2026-10-09 12:41"));

    net.calls.borrow_mut().clear();
    let (r, notes) = look(&mut st, &net); // the status is unchanged (304): one request
    assert!(notes.is_empty() && r.via_status);
    assert_eq!(net.paths(), ["feeds/status.json"]);
}

#[test]
fn only_the_feed_of_a_changed_site_is_read_and_announced_once() {
    let mut st = state(&["a", "b"]);
    let net = Net::new();
    net.put("feeds/status.json", 200, &status_json("2026-10-09T03:41:00Z", &[("a", &head(1)), ("b", &head(1))]), "\"s1\"");
    net.put(&format!("feeds/a.xml?h={}", head(1)), 200, &atom("a", &[1]), "");
    net.put(&format!("feeds/b.xml?h={}", head(1)), 200, &atom("b", &[1]), "");
    look(&mut st, &net);

    // a gets a new update; b does not change
    net.put("feeds/status.json", 200, &status_json("2026-10-09T07:41:00Z", &[("a", &head(2)), ("b", &head(1))]), "\"s2\"");
    net.put(&format!("feeds/a.xml?h={}", head(2)), 200, &atom("a", &[2, 1]), "");
    net.calls.borrow_mut().clear();
    let (r, notes) = look(&mut st, &net);
    assert_eq!(notes.len(), 1);
    assert_eq!(notes[0].slug, "a");
    assert!(notes[0].body.contains("お知らせ2") || notes[0].body.contains("追加 2"), "{}", notes[0].body);
    assert_eq!(r.status_changed, 1);
    assert_eq!(net.paths(), ["feeds/status.json", &format!("feeds/a.xml?h={}", head(2))]); // not b
    assert_eq!(st.generated_ms, jst("2026-10-09 16:41"));

    net.calls.borrow_mut().clear();
    let (_, notes) = look(&mut st, &net); // again: the status is a 304, nothing to announce twice
    assert!(notes.is_empty());
    assert_eq!(net.paths(), ["feeds/status.json"]);
}

#[test]
fn a_site_without_news_is_remembered_so_that_its_first_update_is_announced() {
    let mut st = state(&["a"]);
    let net = Net::new();
    net.put("feeds/status.json", 200, &status_json("2026-10-09T03:41:00Z", &[]), "\"s1\"");
    let (r, notes) = look(&mut st, &net);
    assert!(notes.is_empty());
    assert_eq!(r.first_seen, ["a"]);

    net.put("feeds/status.json", 200, &status_json("2026-10-09T07:41:00Z", &[("a", &head(1))]), "\"s2\"");
    net.put(&format!("feeds/a.xml?h={}", head(1)), 200, &atom("a", &[1]), "");
    let (_, notes) = look(&mut st, &net);
    assert_eq!(notes.len(), 1, "the very first update of a site is news");
}

#[test]
fn without_status_json_every_feed_is_read_as_before() {
    let mut st = state(&["a"]);
    st.status_mode = true; // it worked before, then the site went back to an older build
    let net = Net::new(); // no status.json: 404
    net.put("feeds/a.xml", 200, &atom("a", &[1]), "\"v1\"");
    let (r, _) = look(&mut st, &net);
    assert!(!r.via_status);
    assert!(!st.status_mode);
    assert_eq!(net.paths(), ["feeds/status.json", "feeds/a.xml"]);
}

#[test]
fn a_network_error_changes_nothing() {
    let mut st = state(&["a"]);
    st.last_check = 5_000;
    let net = Net::new();
    *net.down.borrow_mut() = true;
    let (r, notes) = look(&mut st, &net);
    assert!(notes.is_empty() && !r.errors.is_empty());
    assert_eq!(st.last_check, 5_000, "last_check means a check that worked");
    assert!(st.last_error.contains("offline"));
}

#[test]
fn an_old_feed_is_read_again_next_time() {
    let mut st = state(&["a"]);
    let net = Net::new();
    net.put("feeds/status.json", 200, &status_json("2026-10-09T03:41:00Z", &[("a", &head(2))]), "\"s1\"");
    net.put(&format!("feeds/a.xml?h={}", head(2)), 200, &atom("a", &[1]), ""); // the feed does not have the head yet
    let (r, _) = look(&mut st, &net);
    assert!(r.errors.iter().any(|e| e.0 == "a"));
    assert!(st.heads.get("a").is_none(), "the head is not remembered");
    net.put(&format!("feeds/a.xml?h={}", head(2)), 200, &atom("a", &[2, 1]), "");
    net.calls.borrow_mut().clear();
    look(&mut st, &net);
    assert!(net.paths().iter().any(|p| p.starts_with("feeds/a.xml")), "read again");
    assert_eq!(st.heads.get("a").map(String::as_str), Some(head(2).as_str()));
}

#[test]
fn a_new_site_makes_the_names_reload() {
    let mut st = state(&["a"]);
    let net = Net::new();
    net.put("feeds/status.json", 200, &status_json("2026-10-09T03:41:00Z", &[("a", &head(1)), ("zz", &head(1))]), "\"s1\"");
    net.put(&format!("feeds/a.xml?h={}", head(1)), 200, &atom("a", &[1]), "");
    net.put("search.json", 200, r#"{"v":1,"sites":[["a","日本A","https://a/",[]],["zz","新サイト","https://z/",[]]],"updates":[]}"#, "");
    look(&mut st, &net);
    assert!(net.paths().contains(&"search.json".to_string()));
    assert_eq!(st.sites.len(), 2);
}
