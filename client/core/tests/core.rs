use git_core::feed::{html_to_text, parse_atom, parse_follow_input, parse_sites};
use git_core::poller::{poll_once, FetchResult, Fetcher, HttpFetcher, Notice, BURST};
use git_core::state::{self, State};
use std::cell::RefCell;
use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::TcpListener;

const FIXTURE: &str = include_str!("fixtures/jsi-men-eki.xml"); // a real feed of the site

// ---- a feed with the same shape as build_dashboard.py writes ----
struct Item {
    hash: String,
    title: String,
    updated: String,
    li: String,
}

fn hash(n: usize) -> String {
    format!("{n:x}").repeat(40).chars().take(40).collect()
}

fn item(n: usize, text: &str) -> Item {
    Item { hash: hash(n), title: format!("サイト: {text}"), updated: format!("2026-10-07T0{n}:30:00Z"), li: text.to_string() }
}

fn feed(slug: &str, items: &[Item]) -> String {
    let entries: String = items
        .iter()
        .map(|i| {
            format!(
                r#"<entry><title>{}</title><id>tag:git.example.com,2026:g-i-t-data/{slug}/{}</id><updated>{}</updated><content type="html">&lt;ul&gt;&lt;li&gt;{}&lt;/li&gt;&lt;/ul&gt;&lt;p&gt;&lt;a href="https://orig.example/{slug}/"&gt;確認先のページ&lt;/a&gt; ・ &lt;a href="https://git.example.com/sites/{slug}/diff_{}.html"&gt;差分&lt;/a&gt;&lt;/p&gt;</content></entry>"#,
                i.title, i.hash, i.updated, i.li, &i.hash[..7]
            )
        })
        .collect();
    format!(r#"<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>x</title><id>y</id><updated>2026-10-07T00:00:00Z</updated>{entries}</feed>"#)
}

#[test]
fn parse_atom_reads_a_real_feed() {
    let es = parse_atom(FIXTURE).unwrap();
    assert!(es.len() >= 3, "several entries");
    for e in &es {
        assert_eq!(e.slug, "jsi-men-eki");
        assert_eq!(e.hash.len(), 40);
        assert!(e.title.starts_with("日本免疫学会"), "{}", e.title);
        assert!(e.text.chars().count() > 5 && !e.text.contains('<') && !e.text.contains('>'), "{}", e.text);
    }
    assert_eq!(es[0].original_url, "https://www.jsi-men-eki.org/");
    assert!(es[0].updated.starts_with("2026-10-07"));
}

#[test]
fn html_to_text_keeps_bullets_and_drops_links() {
    assert_eq!(html_to_text("<ul><li>追加: A</li><li>変更: B &amp; C</li></ul><p><a href=\"x\">確認先のページ</a></p>"), "追加: A / 変更: B & C");
    assert_eq!(html_to_text("<p>内容に実質的な変更はありません。</p><p><a href=\"x\">確認先のページ</a> ・ <a href=\"y\">差分</a></p>"), "内容に実質的な変更はありません。");
}

#[test]
fn follow_input_variants() {
    assert_eq!(parse_follow_input("https://tanukima.github.io/g-i-t-data/?follow=jsi-men-eki,anesth#x"), ["jsi-men-eki", "anesth"]);
    assert_eq!(parse_follow_input("https://x/?a=1&follow=a%2Cb"), ["a", "b"]);
    assert_eq!(parse_follow_input("a, b\nc  a"), ["a", "b", "c"]);
    assert_eq!(parse_follow_input("Ok_1,../etc,<script>,日本,x y"), ["ok_1", "x", "y"]);
    assert!(parse_follow_input("").is_empty());
}

#[test]
fn sites_from_search_json() {
    let s = parse_sites(r#"{"v":1,"sites":[["a","日本A","https://a.example/",["学会"]],["b","B","https://b.example/",[]]],"updates":[]}"#).unwrap();
    assert_eq!(s.iter().map(|x| x.slug.as_str()).collect::<Vec<_>>(), ["a", "b"]);
    assert_eq!(s[0].name, "日本A");
    assert_eq!(s[0].tags, ["学会"]);
}

// ---- the poller, with a fake network ----
struct FakeFeed {
    xml: String,
    etag: String,
    status: u16,
}

struct Fake {
    feeds: RefCell<HashMap<String, FakeFeed>>,
    calls: RefCell<Vec<(String, Option<String>)>>,
}

impl Fetcher for Fake {
    fn get(&self, url: &str, etag: Option<&str>) -> Result<FetchResult, String> {
        self.calls.borrow_mut().push((url.to_string(), etag.map(String::from)));
        let slug = url.rsplit('/').next().unwrap().trim_end_matches(".xml").to_string();
        let feeds = self.feeds.borrow();
        let Some(f) = feeds.get(&slug) else { return Ok(FetchResult { status: 404, text: String::new(), etag: String::new() }) };
        if f.status != 200 {
            return Ok(FetchResult { status: f.status, text: String::new(), etag: String::new() });
        }
        if etag == Some(f.etag.as_str()) {
            return Ok(FetchResult { status: 304, text: String::new(), etag: f.etag.clone() });
        }
        Ok(FetchResult { status: 200, text: f.xml.clone(), etag: f.etag.clone() })
    }
}

fn harness(slugs: &[&str]) -> (State, Fake) {
    let mut st = State::default();
    st.base_url = "https://dash.example/".into();
    st.follow = slugs.iter().map(|s| s.to_string()).collect();
    st.sites = slugs.iter().map(|s| git_core::feed::Site { slug: s.to_string(), name: "サイト".into(), url: "https://orig.example/".into(), tags: vec![] }).collect();
    st.sites_at = 1_000;
    (st, Fake { feeds: RefCell::new(HashMap::new()), calls: RefCell::new(vec![]) })
}

fn set(fake: &Fake, slug: &str, items: &[Item], etag: &str) {
    fake.feeds.borrow_mut().insert(slug.into(), FakeFeed { xml: feed(slug, items), etag: etag.into(), status: 200 });
}

fn run(st: &mut State, f: &Fake) -> (git_core::poller::PollResult, Vec<Notice>) {
    let mut notes = vec![];
    let r = poll_once(st, f, &mut |n| notes.push(n.clone()), 2_000);
    (r, notes)
}

#[test]
fn first_look_is_silent_and_a_new_update_is_announced_once() {
    let (mut st, f) = harness(&["s1"]);
    set(&f, "s1", &[item(2, "追加: お知らせ"), item(1, "追加: お知らせ")], "\"v1\"");
    let (r, notes) = run(&mut st, &f);
    assert_eq!(r.first_seen, ["s1"]);
    assert!(notes.is_empty());

    set(&f, "s1", &[item(3, "追加: 新しい公示"), item(2, "追加: お知らせ"), item(1, "追加: お知らせ")], "\"v2\"");
    let (_, notes) = run(&mut st, &f);
    assert_eq!(notes.len(), 1);
    let n = &notes[0];
    assert_eq!(n.title, "サイト");
    assert!(n.body.starts_with("2026-10-07 12:30 JST"), "{}", n.body); // 03:30 UTC -> 12:30 JST
    assert!(n.body.contains("新しい公示"));
    assert_eq!(n.diff_url, format!("https://dash.example/sites/s1/diff_{}.html", &hash(3)[..7])); // from the configured base URL, not the feed's host
    assert_eq!(n.history_url, "https://dash.example/sites/s1/history.html");
    assert_eq!(st.recent.len(), 1);
    assert_eq!(st.recent[0].diff_url, n.diff_url);

    let (_, notes) = run(&mut st, &f); // same feed again: a 304, nothing new
    assert!(notes.is_empty());
    assert_eq!(f.calls.borrow().last().unwrap().1.as_deref(), Some("\"v2\""));
}

#[test]
fn an_unchanged_etag_means_no_notification() {
    let (mut st, f) = harness(&["s1"]);
    set(&f, "s1", &[item(1, "追加: a")], "\"a\"");
    run(&mut st, &f);
    let (_, notes) = run(&mut st, &f);
    assert!(notes.is_empty());
    assert_eq!(f.calls.borrow().iter().filter(|c| c.1.as_deref() == Some("\"a\"")).count(), 1);
}

#[test]
fn a_burst_becomes_one_notification() {
    let (mut st, f) = harness(&["s1"]);
    set(&f, "s1", &[item(1, "追加: a")], "\"a\"");
    run(&mut st, &f);
    let mut many: Vec<Item> = (2..=2 + BURST).map(|i| item(i, "追加: x")).collect();
    many.reverse();
    many.push(item(1, "追加: a"));
    set(&f, "s1", &many, "\"b\"");
    let (_, notes) = run(&mut st, &f);
    assert_eq!(notes.len(), 1);
    assert_eq!(notes[0].count, BURST + 1);
    assert!(notes[0].body.contains(&format!("{} 件", BURST + 1)));
    assert!(notes[0].diff_url.contains("diff_"));
}

#[test]
fn one_failing_site_does_not_stop_the_other() {
    let (mut st, f) = harness(&["ok", "bad"]);
    set(&f, "ok", &[item(1, "追加: a")], "\"1\"");
    f.feeds.borrow_mut().insert("bad".into(), FakeFeed { xml: String::new(), etag: String::new(), status: 500 });
    run(&mut st, &f);
    set(&f, "ok", &[item(2, "追加: 通知"), item(1, "追加: a")], "\"2\"");
    let (r, notes) = run(&mut st, &f);
    assert_eq!(notes.len(), 1);
    assert_eq!(r.errors.len(), 1);
    assert_eq!(r.errors[0].0, "bad");
    assert!(st.last_error.contains("bad: HTTP 500"), "{}", st.last_error);
}

#[test]
fn a_site_without_a_feed_is_reported_not_fatal() {
    let (mut st, f) = harness(&["nope"]);
    let (r, _) = run(&mut st, &f);
    assert_eq!(r.errors[0].0, "nope");
}

#[test]
fn entries_of_another_slug_are_ignored() {
    let (mut st, f) = harness(&["s1"]);
    set(&f, "s1", &[item(1, "追加: a")], "\"a\"");
    run(&mut st, &f);
    f.feeds.borrow_mut().insert("s1".into(), FakeFeed { xml: feed("other", &[item(5, "追加: z")]), etag: "\"b\"".into(), status: 200 });
    let (_, notes) = run(&mut st, &f);
    assert!(notes.is_empty());
}

// ---- the real HTTP path against a tiny local server: ETag, 304, 404 ----
#[test]
fn http_fetcher_etag_round_trip() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let server = std::thread::spawn(move || {
        for _ in 0..3 {
            let (mut s, _) = listener.accept().unwrap();
            let mut buf = [0u8; 2048];
            let n = s.read(&mut buf).unwrap();
            let req = String::from_utf8_lossy(&buf[..n]).to_lowercase();
            let reply = if req.starts_with("get /feeds/x.xml") {
                if req.contains("if-none-match: \"e1\"") {
                    "HTTP/1.1 304 Not Modified\r\nConnection: close\r\n\r\n".to_string()
                } else {
                    "HTTP/1.1 200 OK\r\nETag: \"e1\"\r\nContent-Length: 7\r\nConnection: close\r\n\r\n<feed/>".to_string()
                }
            } else {
                "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".to_string()
            };
            s.write_all(reply.as_bytes()).unwrap();
        }
    });
    let f = HttpFetcher::new();
    let base = format!("http://127.0.0.1:{port}");
    let a = f.get(&format!("{base}/feeds/x.xml"), None).unwrap();
    assert_eq!((a.status, a.etag.as_str(), a.text.as_str()), (200, "\"e1\"", "<feed/>"));
    let b = f.get(&format!("{base}/feeds/x.xml"), Some("\"e1\"")).unwrap();
    assert_eq!(b.status, 304);
    assert_eq!(f.get(&format!("{base}/feeds/none.xml"), None).unwrap().status, 404);
    server.join().unwrap();
}

#[test]
fn a_move_to_another_domain_does_not_make_every_entry_new() {
    // an old state: the old default address, entries remembered with the whole id (host name included)
    let dir = std::env::temp_dir().join(format!("gd-move-{}", std::process::id()));
    let file = dir.join("state.json");
    let mut old = State::default();
    old.base_url = state::OLD_DEFAULT_BASE_URL.to_string();
    old.follow = vec!["s1".into()];
    old.sites = vec![git_core::feed::Site { slug: "s1".into(), name: "サイト".into(), url: "https://orig.example/".into(), tags: vec![] }];
    old.sites_at = 1_000;
    old.etags.insert("s1".into(), "\"old\"".into());
    old.seen.insert("s1".into(), vec![format!("tag:tanukima.github.io,2026:g-i-t-data/s1/{}", hash(2)), format!("tag:tanukima.github.io,2026:g-i-t-data/s1/{}", hash(1))]);
    state::save(&file, &old).unwrap();

    let mut st = state::load(&file);
    assert_eq!(st.base_url, state::DEFAULT_BASE_URL, "the old default address is moved");
    assert!(st.etags.is_empty(), "ETags of the old address are dropped");
    assert_eq!(st.seen["s1"], [format!("s1/{}", hash(2)), format!("s1/{}", hash(1))], "ids are reduced to <slug>/<commit>");

    // the feed now carries ids of the NEW host: the same two updates, and one that is really new
    let f = Fake { feeds: RefCell::new(HashMap::new()), calls: RefCell::new(vec![]) };
    let xml = feed("s1", &[item(3, "追加: 本当に新しい"), item(2, "追加: 既知"), item(1, "追加: 既知")]).replace("tag:git.example.com", "tag:giiit.goudge.org");
    f.feeds.borrow_mut().insert("s1".into(), FakeFeed { xml, etag: "\"n\"".into(), status: 200 });
    let (_, notes) = run(&mut st, &f);
    assert_eq!(notes.len(), 1, "only the new update is announced, not the whole feed");
    assert!(notes[0].body.contains("本当に新しい"));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn entry_keys_do_not_depend_on_the_host() {
    use git_core::feed::seen_key;
    assert_eq!(seen_key("tag:giiit.goudge.org,2026:g-i-t-data/mhlw/abc1234"), "mhlw/abc1234");
    assert_eq!(seen_key("tag:tanukima.github.io,2026:g-i-t-data/mhlw/abc1234"), "mhlw/abc1234");
    assert_eq!(seen_key("id1"), "id1", "an id without the marker stays as it is");
}

#[test]
fn state_round_trip_and_normalize_base() {
    let dir = std::env::temp_dir().join(format!("gd-{}", std::process::id()));
    let file = dir.join("sub").join("state.json");
    let mut s = State::default();
    s.follow = vec!["a".into()];
    s.seen.insert("a".into(), vec!["id1".into()]);
    state::save(&file, &s).unwrap();
    let back = state::load(&file);
    assert_eq!(back.follow, ["a"]);
    assert_eq!(back.seen["a"], ["id1"]);
    assert_eq!(state::load(&dir.join("missing.json")).interval_min, 15);
    assert_eq!(state::normalize_base("https://x.example/g").unwrap(), "https://x.example/g/");
    assert!(state::normalize_base("ftp://x").is_err());
    let _ = std::fs::remove_dir_all(&dir);
}
