//! Parsing of what the G醫t site publishes.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// One update, as an entry of `feeds/<slug>.xml`.
#[derive(Debug, Clone, PartialEq)]
pub struct Entry {
    pub id: String,
    pub slug: String,
    /// The commit hash from the entry id (7 to 64 hex digits); the diff page is `diff_<first 7>.html`.
    pub hash: String,
    pub title: String,
    /// RFC 3339, as written in the feed (UTC).
    pub updated: String,
    /// The visible text of the summary.
    pub text: String,
    pub original_url: String,
}

/// One monitored site, from `search.json`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct Site {
    pub slug: String,
    pub name: String,
    pub url: String,
    pub tags: Vec<String>,
}

pub fn is_slug(s: &str) -> bool {
    let mut chars = s.chars();
    match chars.next() {
        Some(c) if c.is_ascii_lowercase() || c.is_ascii_digit() => {}
        _ => return false,
    }
    chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-')
}

/// "tag:HOST,2026:g-i-t-data/<slug>/<commit>" -> (slug, commit)
fn split_id(id: &str) -> Option<(String, String)> {
    let rest = &id[id.find("g-i-t-data/")? + "g-i-t-data/".len()..];
    let (slug, hash) = rest.split_once('/')?;
    let hash_ok = (7..=64).contains(&hash.len()) && hash.chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase());
    (is_slug(slug) && hash_ok).then(|| (slug.to_string(), hash.to_string()))
}

fn decode_entities(s: &str) -> String {
    s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", "\"").replace("&#39;", "'").replace("&amp;", "&")
}

/// The visible text of the summary HTML. The last paragraph holds the links ("確認先のページ ・ 差分"): it is dropped.
pub fn html_to_text(html: &str) -> String {
    let mut body = html;
    if let Some(at) = html.rfind("<p>") {
        if html[at..].contains("確認先のページ") {
            body = &html[..at];
        }
    }
    let body = body.replace("</li><li>", " / ").replace("</p><p>", " ");
    let mut out = String::with_capacity(body.len());
    let mut in_tag = false;
    for c in body.chars() {
        match c {
            '<' => in_tag = true,
            '>' if in_tag => in_tag = false,
            _ if !in_tag => out.push(c),
            _ => {}
        }
    }
    decode_entities(&out).split_whitespace().collect::<Vec<_>>().join(" ")
}

fn original_url(content: &str) -> String {
    let Some(at) = content.find("確認先のページ") else { return String::new() };
    let before = &content[..at];
    let Some(h) = before.rfind("href=\"") else { return String::new() };
    let rest = &before[h + 6..];
    rest.split('"').next().map(decode_entities).unwrap_or_default()
}

/// The entries of an Atom feed written by build_dashboard.py, in the order of the file (newest first).
/// Entries whose id is not one of ours are skipped.
pub fn parse_atom(xml: &str) -> Result<Vec<Entry>, String> {
    let doc = roxmltree::Document::parse(xml).map_err(|e| format!("Atom: {e}"))?;
    let text_of = |node: roxmltree::Node, name: &str| -> String {
        node.children().find(|n| n.is_element() && n.tag_name().name() == name).and_then(|n| n.text()).unwrap_or("").to_string()
    };
    let mut out = Vec::new();
    for e in doc.descendants().filter(|n| n.is_element() && n.tag_name().name() == "entry") {
        let id = text_of(e, "id");
        let Some((slug, hash)) = split_id(&id) else { continue };
        let content = {
            let c = text_of(e, "content");
            if c.is_empty() { text_of(e, "summary") } else { c }
        };
        out.push(Entry {
            id,
            slug,
            hash,
            title: text_of(e, "title"),
            updated: text_of(e, "updated"),
            text: html_to_text(&content),
            original_url: original_url(&content),
        });
    }
    Ok(out)
}

/// `feeds/status.json`: when the pipeline runs and the newest update of every site (see build_dashboard.py).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Status {
    /// When the last run finished building the dashboard (Unix ms).
    pub generated_ms: i64,
    pub runs: Vec<u32>,
    pub offset_min: i32,
    pub delay_min: u32,
    pub window_min: u32,
    /// slug -> the 7-character hash of the newest real update of the site.
    pub sites: HashMap<String, String>,
}

fn parse_hhmm(s: &str) -> Option<u32> {
    let (h, m) = s.split_once(':')?;
    let (h, m): (u32, u32) = (h.parse().ok()?, m.parse().ok()?);
    (h < 24 && m < 60).then_some(h * 60 + m)
}

pub fn parse_status(json: &str) -> Result<Status, String> {
    let v: serde_json::Value = serde_json::from_str(json).map_err(|e| format!("status.json: {e}"))?;
    let generated = v.get("generated").and_then(|g| g.as_str()).ok_or("status.json: no `generated`")?;
    let generated_ms = chrono::DateTime::parse_from_rfc3339(generated).map_err(|e| format!("status.json: {e}"))?.timestamp_millis();
    let mut runs: Vec<u32> = v.get("runs").and_then(|r| r.as_array()).map(|r| r.iter().filter_map(|x| x.as_str().and_then(parse_hhmm)).collect()).unwrap_or_default();
    runs.sort_unstable();
    runs.dedup();
    if runs.is_empty() {
        return Err("status.json: no `runs`".into());
    }
    let num = |key: &str, default: u64| v.get(key).and_then(|x| x.as_u64()).unwrap_or(default);
    let sites = v
        .get("sites")
        .and_then(|s| s.as_object())
        .map(|o| o.iter().filter(|(k, _)| is_slug(k)).filter_map(|(k, h)| Some((k.clone(), h.as_str()?.to_string()))).collect())
        .ok_or("status.json: no `sites`")?;
    Ok(Status {
        generated_ms,
        runs,
        offset_min: v.get("utcOffsetMin").and_then(|x| x.as_i64()).unwrap_or(540).clamp(-720, 840) as i32,
        delay_min: num("delayMin", 8).min(120) as u32,
        window_min: num("windowMin", 25).clamp(5, 180) as u32,
        sites,
    })
}

/// `search.json` -> the sites: `{"sites": [[slug, name, url, [tags]], ...], ...}`
pub fn parse_sites(json: &str) -> Result<Vec<Site>, String> {
    let v: serde_json::Value = serde_json::from_str(json).map_err(|e| format!("search.json: {e}"))?;
    let list = v.get("sites").and_then(|s| s.as_array()).ok_or("search.json: no `sites`")?;
    Ok(list
        .iter()
        .filter_map(|row| {
            let r = row.as_array()?;
            Some(Site {
                slug: r.first()?.as_str()?.to_string(),
                name: r.get(1)?.as_str()?.to_string(),
                url: r.get(2).and_then(|x| x.as_str()).unwrap_or("").to_string(),
                tags: r.get(3).and_then(|t| t.as_array()).map(|t| t.iter().filter_map(|x| x.as_str().map(String::from)).collect()).unwrap_or_default(),
            })
        })
        .collect())
}

fn percent_decode(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' && i + 2 < b.len() {
            if let Some(v) = std::str::from_utf8(&b[i + 1..i + 3]).ok().and_then(|h| u8::from_str_radix(h, 16).ok()) {
                out.push(v);
                i += 3;
                continue;
            }
        }
        out.push(b[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// A share URL (`…/?follow=a,b,c`), "a,b,c", or a list separated by spaces / new lines -> the valid, unique slugs.
pub fn parse_follow_input(text: &str) -> Vec<String> {
    let t = text.trim();
    let list = match t.find("follow=") {
        Some(at) if t[..at].ends_with('?') || t[..at].ends_with('&') => {
            let v = &t[at + 7..];
            let end = v.find(|c: char| c == '&' || c == '#' || c.is_whitespace()).unwrap_or(v.len());
            percent_decode(&v[..end])
        }
        _ => t.to_string(),
    };
    let mut out: Vec<String> = Vec::new();
    for part in list.split(|c: char| c == ',' || c.is_whitespace()) {
        let s = part.trim().to_lowercase();
        if is_slug(&s) && !out.contains(&s) {
            out.push(s);
        }
    }
    out
}
