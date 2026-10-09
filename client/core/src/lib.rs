//! G醫t client core. Everything that is the same on Windows, macOS, Android and iOS lives here: parsing what the site
//! publishes (the per-site Atom feeds and `search.json`), the small local state, and one check of all followed sites.
//! No Tauri, no OS calls: the platform shells only hand in a `Fetcher` and a notification callback.

pub mod feed;
pub mod poller;
pub mod schedule;
pub mod state;
