"""Drain archive_queue: register pending URLs with the Internet Archive one at a time."""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    import savepagenow
    from savepagenow.exceptions import BlockedByRobots, TooManyRequests
except ImportError:
    savepagenow = None

try:
    from supabase import create_client
except ImportError:
    create_client = None

BATCH_SIZE = int(os.environ.get("ARCHIVE_BATCH_SIZE", "20"))
INTERVAL_SEC = int(os.environ.get("ARCHIVE_INTERVAL_SEC", "15"))
MAX_ATTEMPTS = 5
BACKOFF_MINUTES = 30      # doubled per failed attempt
RATE_LIMIT_MINUTES = 15   # retry delay after HTTP 429


def now():
    return datetime.now(timezone.utc)


def main():
    supa_url = os.environ.get("SUPABASE_URL")
    supa_key = os.environ.get("SUPABASE_KEY")
    ia_access = os.environ.get("IA_ACCESS_KEY")
    ia_secret = os.environ.get("IA_SECRET_KEY")
    if not (savepagenow and create_client and supa_url and supa_key and ia_access and ia_secret):
        print("Missing modules or credentials (SUPABASE_*, IA_*).")
        sys.exit(1)

    # savepagenow reads its credentials from these env vars, not from arguments.
    os.environ["SAVEPAGENOW_ACCESS_KEY"] = ia_access
    os.environ["SAVEPAGENOW_SECRET_KEY"] = ia_secret

    supabase = create_client(supa_url, supa_key)
    rows = (supabase.table("archive_queue").select("*")
            .eq("status", "pending").lte("next_try_at", now().isoformat())
            .order("next_try_at").limit(BATCH_SIZE).execute().data or [])
    print(f"{len(rows)} pending URL(s) (batch size {BATCH_SIZE}, interval {INTERVAL_SEC}s).")

    def update(row_id, **fields):
        try:
            supabase.table("archive_queue").update(fields).eq("id", row_id).execute()
        except Exception as e:
            print(f"Failed to update queue row {row_id}: {e}")

    for i, row in enumerate(rows):
        if i:
            time.sleep(INTERVAL_SEC)
        url, attempts = row["url"], row["attempts"] + 1
        try:
            archive_url, _ = savepagenow.capture_or_cache(url, authenticate=True)
            print(f"Archived {url} -> {archive_url}")
            update(row["id"], status="done", archive_url=archive_url, attempts=attempts, last_error=None)
        except TooManyRequests as e:
            print(f"Rate limited at {url}; stopping this batch.")
            update(row["id"], next_try_at=(now() + timedelta(minutes=RATE_LIMIT_MINUTES)).isoformat(),
                   last_error=f"429: {e}"[:500])
            break
        except BlockedByRobots as e:
            print(f"Blocked by robots.txt: {url}")
            update(row["id"], status="failed", attempts=attempts, last_error=f"robots: {e}"[:500])
        except Exception as e:
            print(f"Error archiving {url}: {e}")
            if attempts >= MAX_ATTEMPTS:
                update(row["id"], status="failed", attempts=attempts, last_error=str(e)[:500])
            else:
                delay = timedelta(minutes=BACKOFF_MINUTES * 2 ** (attempts - 1))
                update(row["id"], attempts=attempts, last_error=str(e)[:500],
                       next_try_at=(now() + delay).isoformat())


if __name__ == "__main__":
    main()
