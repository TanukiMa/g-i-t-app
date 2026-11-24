"""Supabase: one client per process, and the reads and writes of the `updates` and `archive_queue` tables that more than
one script needs. Every write here catches its own errors (a database problem must never stop a run); callers that must
know get a return value."""
import os
import threading

try:
    from supabase import create_client
except ImportError:
    create_client = None

PAGE_SIZE = 1000
_client = {}
_lock = threading.Lock()


def get_supabase():
    """The Supabase client (made once per process), or None when SUPABASE_URL / SUPABASE_KEY or the module are missing."""
    with _lock:
        if "c" not in _client:
            supa_url, supa_key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY")
            if not create_client or not supa_url or not supa_key:
                print("Supabase URL/Key or supabase module missing. Skipping DB access.")
                _client["c"] = None
            else:
                _client["c"] = create_client(supa_url, supa_key)
        return _client["c"]


def fetch_all(supabase, table: str) -> list:
    """Page through a table, newest id first; PostgREST caps each response (1000 by default)."""
    rows = []
    while True:
        res = (supabase.table(table).select("*")
               .order("id", desc=True)
               .range(len(rows), len(rows) + PAGE_SIZE - 1).execute())
        rows.extend(res.data or [])
        if len(res.data or []) < PAGE_SIZE:
            return rows


def log_to_supabase(data_dict: dict):
    """Insert one row into `updates`. Returns the new row's id (None when it could not be stored)."""
    try:
        supabase = get_supabase()
        if supabase:
            res = supabase.table("updates").insert(data_dict).execute()
            print(f"Logged to Supabase: {data_dict.get('site_slug')} {str(data_dict.get('commit_hash'))[:7]}")
            rows = getattr(res, "data", None) or []
            return rows[0].get("id") if rows and isinstance(rows[0], dict) else None
    except Exception as e:
        print(f"Error inserting into Supabase: {e}")
    return None


def write_summary(supabase, row_id, summary: str, model=None):
    """Store a summary and the model that wrote it. Before sql/add-summary-model.sql was run the column does not
    exist: the summary is then stored without it (a summary must never be lost because of the model name)."""
    if model:
        try:
            supabase.table("updates").update({"summary": summary, "summary_model": model}).eq("id", row_id).execute()
            return
        except Exception as e:
            print(f"Could not store summary_model for update {row_id} ({e}); storing the summary alone.")
    supabase.table("updates").update({"summary": summary}).eq("id", row_id).execute()


def set_summary(row_id, summary: str, model=None) -> bool:
    try:
        supabase = get_supabase()
        if supabase and row_id is not None:
            write_summary(supabase, row_id, summary, model)
            return True
    except Exception as e:
        print(f"Error updating summary of update {row_id}: {e}")
    return False


def enqueue_archives(site_slug: str, commit_hash: str, page_url: str, attachment_urls: list):
    """Queue URLs for archive_worker.py; the Wayback calls happen there, not here."""
    rows = [{"site_slug": site_slug, "commit_hash": commit_hash, "url": page_url, "kind": "page"}]
    rows += [{"site_slug": site_slug, "commit_hash": commit_hash, "url": u, "kind": "attachment"}
             for u in attachment_urls]
    try:
        supabase = get_supabase()
        if supabase:
            supabase.table("archive_queue").upsert(
                rows, on_conflict="site_slug,commit_hash,url", ignore_duplicates=True
            ).execute()
            print(f"Queued {len(rows)} URL(s) for archiving.")
    except Exception as e:
        print(f"Error queueing archive URLs: {e}")
