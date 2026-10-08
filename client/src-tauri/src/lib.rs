//! G醫t client (Tauri 2). A thin shell around `git_core`: it keeps the state on this device, checks the feeds of the
//! followed sites on a timer, shows the OS notifications and offers the settings window. Nothing is sent anywhere.
//!
//! Desktop (Windows / macOS): tray icon, check on a timer, start at login. Mobile: the same core and screen; what happens
//! while the app is closed is decided by the OS (see client/README.md).

use git_core::feed::{parse_follow_input, Site};
use git_core::poller::{poll_once, refresh_sites, HttpFetcher, Notice};
use git_core::state::{self, Recent, State};
use serde::Serialize;
use std::path::PathBuf;
use std::sync::mpsc::{channel, Sender};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tauri::{AppHandle, Manager};
use tauri_plugin_notification::NotificationExt;
use tauri_plugin_opener::OpenerExt;

/// Everything the commands and the checking thread share.
struct Shared {
    state: Mutex<State>,
    file: PathBuf,
    wake: Mutex<Sender<()>>,
}

type SharedState = Arc<Shared>;

fn now_ms() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
}

fn save(shared: &Shared, state: &State) {
    let _ = state::save(&shared.file, state);
}

/// Only http(s) addresses are ever handed to the OS browser.
fn open_url(app: &AppHandle, url: &str) {
    let l = url.to_lowercase();
    if l.starts_with("https://") || l.starts_with("http://") {
        let _ = app.opener().open_url(url, None::<&str>);
    }
}

fn show_notice(app: &AppHandle, n: &Notice) {
    let _ = app.notification().builder().title(&n.title).body(&n.body).show();
}

/// One check: works on a copy of the state (the network can be slow), then merges what it found.
fn check(app: &AppHandle, shared: &Shared) {
    let mut polled = shared.state.lock().unwrap().clone();
    let fetcher = HttpFetcher::new();
    let _ = poll_once(&mut polled, &fetcher, &mut |n| show_notice(app, n), now_ms());
    let snapshot = {
        let mut s = shared.state.lock().unwrap();
        s.merge_poll(polled);
        save(shared, &s);
        s.clone()
    };
    #[cfg(desktop)]
    desktop::refresh_tray(app, &snapshot);
    let _ = &snapshot;
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct View {
    base_url: String,
    interval_min: u32,
    follow: Vec<String>,
    sites: Vec<Site>,
    recent: Vec<Recent>,
    open_at_login: bool,
    last_check: i64,
    last_error: String,
}

fn view(app: &AppHandle, s: &State) -> View {
    #[cfg(desktop)]
    let open_at_login = {
        use tauri_plugin_autostart::ManagerExt;
        app.autolaunch().is_enabled().unwrap_or(false)
    };
    #[cfg(not(desktop))]
    let open_at_login = false;
    let _ = app;
    View {
        base_url: s.base_url.clone(),
        interval_min: s.interval_min,
        follow: s.follow.clone(),
        sites: s.sites.clone(),
        recent: s.recent.clone(),
        open_at_login,
        last_check: s.last_check,
        last_error: s.last_error.clone(),
    }
}

// ---- the commands the settings window may call (async: the main thread must never wait for the network) ----

#[tauri::command]
async fn get_state(app: AppHandle, shared: tauri::State<'_, SharedState>) -> Result<View, String> {
    let shared = shared.inner().clone();
    let need_names = shared.state.lock().unwrap().sites.is_empty();
    if need_names {
        let sh = shared.clone();
        let _ = tauri::async_runtime::spawn_blocking(move || {
            let mut copy = sh.state.lock().unwrap().clone();
            if refresh_sites(&mut copy, &HttpFetcher::new(), now_ms()).is_ok() {
                let mut s = sh.state.lock().unwrap();
                s.sites = copy.sites;
                s.sites_at = copy.sites_at;
                save(&sh, &s);
            }
        })
        .await;
    }
    let s = shared.state.lock().unwrap().clone();
    Ok(view(&app, &s))
}

#[tauri::command]
fn set_follow(app: AppHandle, shared: tauri::State<'_, SharedState>, slugs: Vec<String>) -> Vec<String> {
    let follow = parse_follow_input(&slugs.join(","));
    let mut s = shared.state.lock().unwrap();
    s.follow = follow.clone();
    save(&shared, &s);
    #[cfg(desktop)]
    desktop::refresh_tray(&app, &s);
    let _ = &app;
    follow
}

#[derive(Serialize)]
struct Imported {
    added: usize,
    follow: Vec<String>,
}

#[tauri::command]
fn import_follow(app: AppHandle, shared: tauri::State<'_, SharedState>, text: String) -> Imported {
    let add = parse_follow_input(&text);
    let mut s = shared.state.lock().unwrap();
    for a in &add {
        if !s.follow.contains(a) {
            s.follow.push(a.clone());
        }
    }
    save(&shared, &s);
    #[cfg(desktop)]
    desktop::refresh_tray(&app, &s);
    let _ = &app;
    Imported { added: add.len(), follow: s.follow.clone() }
}

#[tauri::command]
fn set_options(app: AppHandle, shared: tauri::State<'_, SharedState>, base_url: Option<String>, interval_min: Option<u32>, open_at_login: Option<bool>) -> Result<(), String> {
    let mut s = shared.state.lock().unwrap();
    if let Some(u) = base_url {
        let next = state::normalize_base(&u)?;
        if next != s.base_url {
            s.base_url = next; // another site: start fresh
            s.sites.clear();
            s.sites_at = 0;
            s.seen.clear();
            s.etags.clear();
            s.recent.clear();
        }
    }
    if let Some(m) = interval_min {
        s.interval_min = m.clamp(5, 240);
    }
    #[cfg(desktop)]
    if let Some(on) = open_at_login {
        use tauri_plugin_autostart::ManagerExt;
        let _ = if on { app.autolaunch().enable() } else { app.autolaunch().disable() };
    }
    let _ = (&app, open_at_login);
    save(&shared, &s);
    let _ = shared.wake.lock().unwrap().send(()); // re-read the interval
    Ok(())
}

#[tauri::command]
fn check_now(shared: tauri::State<'_, SharedState>) {
    let _ = shared.wake.lock().unwrap().send(());
}

#[tauri::command]
fn test_notification(app: AppHandle) {
    let _ = app.notification().builder().title("G醫t").body("通知のテストです。").show();
}

#[tauri::command]
fn open_external(app: AppHandle, url: String) {
    open_url(&app, &url);
}

/// The checking thread: waits for the interval (or a wake-up from "check now"), then checks.
fn spawn_checker(app: AppHandle, shared: SharedState, rx: std::sync::mpsc::Receiver<()>) {
    std::thread::spawn(move || {
        let mut wait = Duration::from_secs(10); // the first check shortly after the start
        loop {
            let manual = matches!(rx.recv_timeout(wait), Ok(()));
            let (interval, follows) = {
                let s = shared.state.lock().unwrap();
                (Duration::from_secs(60 * s.interval_min.clamp(5, 240) as u64), !s.follow.is_empty())
            };
            if follows || manual {
                check(&app, &shared);
            }
            wait = interval;
        }
    });
}

#[cfg(desktop)]
mod desktop {
    use super::*;
    use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem, Submenu};
    use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
    use tauri_plugin_autostart::ManagerExt;

    pub fn show_window(app: &AppHandle) {
        if let Some(w) = app.get_webview_window("main") {
            let _ = w.show();
            let _ = w.unminimize();
            let _ = w.set_focus();
        }
    }

    fn build_menu(app: &AppHandle, s: &State) -> tauri::Result<Menu<tauri::Wry>> {
        let when = if s.last_check == 0 { "まだ確認していません".to_string() } else { format!("最終確認: {}", format_local(s.last_check)) };
        let mut recent_items: Vec<MenuItem<tauri::Wry>> = Vec::new();
        for (i, r) in s.recent.iter().take(8).enumerate() {
            let label = format!("{}  {}", r.name, r.time);
            recent_items.push(MenuItem::with_id(app, format!("open:{i}"), label, true, None::<&str>)?);
        }
        let recent_refs: Vec<&dyn tauri::menu::IsMenuItem<tauri::Wry>> = recent_items.iter().map(|i| i as &dyn tauri::menu::IsMenuItem<tauri::Wry>).collect();
        let recent = Submenu::with_items(app, "最近の更新（クリックで差分を開く）", !recent_items.is_empty(), &recent_refs)?;
        let login = CheckMenuItem::with_id(app, "login", "ログイン時に起動", true, app.autolaunch().is_enabled().unwrap_or(false), None::<&str>)?;
        Menu::with_items(
            app,
            &[
                &MenuItem::with_id(app, "info", format!("フォロー中: {} サイト", s.follow.len()), false, None::<&str>)?,
                &MenuItem::with_id(app, "when", when, false, None::<&str>)?,
                &PredefinedMenuItem::separator(app)?,
                &MenuItem::with_id(app, "check", "今すぐ確認", true, None::<&str>)?,
                &recent,
                &MenuItem::with_id(app, "settings", "設定・サイトの選択…", true, None::<&str>)?,
                &MenuItem::with_id(app, "dashboard", "ダッシュボードを開く", true, None::<&str>)?,
                &PredefinedMenuItem::separator(app)?,
                &login,
                &PredefinedMenuItem::separator(app)?,
                &MenuItem::with_id(app, "quit", "終了", true, None::<&str>)?,
            ],
        )
    }

    fn format_local(ms: i64) -> String {
        // "MM-DD HH:MM" in JST: the same clock as everywhere else in G醫t
        let secs = ms / 1000 + 9 * 3600;
        let days = secs.div_euclid(86400);
        let rem = secs.rem_euclid(86400);
        let (y, m, d) = civil_from_days(days);
        format!("{y:04}-{m:02}-{d:02} {:02}:{:02} JST", rem / 3600, (rem % 3600) / 60)
    }

    // days since 1970-01-01 -> (year, month, day), proleptic Gregorian (Howard Hinnant's algorithm)
    fn civil_from_days(z: i64) -> (i64, i64, i64) {
        let z = z + 719468;
        let era = z.div_euclid(146097);
        let doe = z.rem_euclid(146097);
        let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
        let y = yoe + era * 400;
        let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
        let mp = (5 * doy + 2) / 153;
        let d = doy - (153 * mp + 2) / 5 + 1;
        let m = if mp < 10 { mp + 3 } else { mp - 9 };
        (if m <= 2 { y + 1 } else { y }, m, d)
    }

    pub fn refresh_tray(app: &AppHandle, s: &State) {
        if let Some(tray) = app.tray_by_id("main") {
            if let Ok(menu) = build_menu(app, s) {
                let _ = tray.set_menu(Some(menu));
            }
            let _ = tray.set_tooltip(Some(format!("G醫t（{} サイトをフォロー中）", s.follow.len())));
        }
    }

    pub fn setup(app: &AppHandle, shared: &SharedState) -> tauri::Result<()> {
        let menu = build_menu(app, &shared.state.lock().unwrap())?;
        let shared_for_menu = shared.clone();
        TrayIconBuilder::with_id("main")
            .icon(app.default_window_icon().cloned().expect("window icon"))
            .tooltip("G醫t")
            .menu(&menu)
            .show_menu_on_left_click(false)
            .on_tray_icon_event(|tray, event| {
                if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event {
                    show_window(tray.app_handle());
                }
            })
            .on_menu_event(move |app, event| {
                let id = event.id().as_ref().to_string();
                match id.as_str() {
                    "check" => {
                        let _ = shared_for_menu.wake.lock().unwrap().send(());
                    }
                    "settings" => show_window(app),
                    "dashboard" => {
                        let url = shared_for_menu.state.lock().unwrap().base_url.clone();
                        open_url(app, &url);
                    }
                    "login" => {
                        let al = app.autolaunch();
                        let _ = if al.is_enabled().unwrap_or(false) { al.disable() } else { al.enable() };
                        let s = shared_for_menu.state.lock().unwrap().clone();
                        refresh_tray(app, &s);
                    }
                    "quit" => app.exit(0),
                    other if other.starts_with("open:") => {
                        if let Ok(i) = other[5..].parse::<usize>() {
                            let url = shared_for_menu.state.lock().unwrap().recent.get(i).map(|r| if r.count > 1 { r.history_url.clone() } else { r.diff_url.clone() });
                            if let Some(url) = url {
                                open_url(app, &url);
                            }
                        }
                    }
                    _ => {}
                }
            })
            .build(app)?;
        Ok(())
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let mut builder = tauri::Builder::default();

    #[cfg(desktop)]
    {
        builder = builder
            .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| desktop::show_window(app)))
            .plugin(tauri_plugin_autostart::init(tauri_plugin_autostart::MacosLauncher::LaunchAgent, None::<Vec<&'static str>>));
    }

    builder
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            let dir = std::env::var("GIT_CLIENT_DATA").map(PathBuf::from).unwrap_or_else(|_| app.path().app_data_dir().expect("app data dir"));
            let file = dir.join("state.json");
            let (tx, rx) = channel::<()>();
            let shared: SharedState = Arc::new(Shared { state: Mutex::new(state::load(&file)), file, wake: Mutex::new(tx) });
            app.manage(shared.clone());
            spawn_checker(app.handle().clone(), shared.clone(), rx);
            #[cfg(desktop)]
            {
                desktop::setup(app.handle(), &shared)?;
                if shared.state.lock().unwrap().follow.is_empty() {
                    desktop::show_window(app.handle()); // first start: choose the sites
                }
            }
            #[cfg(mobile)]
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            // The window only hides: the app stays in the tray / keeps checking
            #[cfg(desktop)]
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
            let _ = (window, event);
        })
        .invoke_handler(tauri::generate_handler![get_state, set_follow, import_follow, set_options, check_now, test_notification, open_external])
        .run(tauri::generate_context!())
        .expect("error while running G醫t client");
}
