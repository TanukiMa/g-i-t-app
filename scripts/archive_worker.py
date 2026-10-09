"""Drain archive_queue: register pending URLs with the Internet Archive, one at a time.

GitHub only fires a "*/15" schedule a few times a day, so one run must not stop after a fixed handful of
URLs: it keeps working until nothing is due any more or the time budget is used up.

Two modes: with the S3 keys the Internet Archive allows a higher rate. When even that limit is reached (HTTP 429)
the worker does not stop but switches to the unauthenticated mode and goes on slowly (ARCHIVE_ANON_INTERVAL_SEC),
trying the keys again after ARCHIVE_AUTH_RETRY_MIN minutes. It stops on a 429 only when the unauthenticated
mode is limited as well (or ARCHIVE_ANON_FALLBACK=0).

The same URL can be queued several times (a page that was updated twice before the worker ran; one commit per update).
A URL is registered ONCE per run and the result is shared with every other due row of that URL (no row is deleted:
each commit keeps its own row and Wayback link). The rows that are not due yet, and rows that are already done, are
not touched: a later commit must not get an older capture.
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    import savepagenow
    from savepagenow.exceptions import BlockedByRobots, TooManyRequests, Unauthorized
except ImportError:
    savepagenow = None

try:
    from supabase import create_client
except ImportError:
    create_client = None

BATCH_SIZE = int(os.environ.get("ARCHIVE_BATCH_SIZE", "20"))        # rows fetched per round
INTERVAL_SEC = int(os.environ.get("ARCHIVE_INTERVAL_SEC", "30"))    # pause between two URLs
RUNTIME_MINUTES = float(os.environ.get("ARCHIVE_RUNTIME_MIN") or 14)  # stop starting new URLs after this
MAX_ATTEMPTS = 5
BACKOFF_MINUTES = 30      # doubled per failed attempt
RATE_LIMIT_MINUTES = 15   # retry delay after HTTP 429
# Unauthenticated fallback after the authenticated rate limit (0 = off: stop at the first 429 as before).
ANON_FALLBACK = os.environ.get("ARCHIVE_ANON_FALLBACK", "1") != "0"
ANON_INTERVAL_SEC = int(os.environ.get("ARCHIVE_ANON_INTERVAL_SEC") or 60)   # slower pace without keys
AUTH_RETRY_MINUTES = float(os.environ.get("ARCHIVE_AUTH_RETRY_MIN") or 15)   # then try the keys again


def now():
    return datetime.now(timezone.utc)


def describe(error) -> str:
    """Exception class + message: savepagenow's errors only carry headers, so the class is what tells them apart."""
    return f"{type(error).__name__}: {error}"[:500]


def due_rows(supabase, limit: int) -> list:
    return (supabase.table("archive_queue").select("*")
            .eq("status", "pending").lte("next_try_at", now().isoformat())
            .order("next_try_at").limit(limit).execute().data or [])


def update_row(supabase, row_id, **fields):
    try:
        supabase.table("archive_queue").update(fields).eq("id", row_id).execute()
    except Exception as e:
        print(f"Failed to update queue row {row_id}: {e}")


def sibling_rows(supabase, row) -> list:
    """Other rows with the same URL that are pending and due now (they would be tried again in this run)."""
    try:
        return (supabase.table("archive_queue").select("id,attempts").eq("status", "pending").eq("url", row["url"])
                .neq("id", row["id"]).lte("next_try_at", now().isoformat()).execute().data or [])
    except Exception as e:
        print(f"Could not look for other rows of {row['url']}: {e}")
        return []


def retry_fields(attempts: int, error) -> dict:
    """Fields for a row whose capture failed: back off, or give up after MAX_ATTEMPTS."""
    if attempts >= MAX_ATTEMPTS:
        return {"status": "failed", "attempts": attempts, "last_error": describe(error)}
    delay = timedelta(minutes=BACKOFF_MINUTES * 2 ** (attempts - 1))
    return {"attempts": attempts, "last_error": describe(error), "next_try_at": (now() + delay).isoformat()}


def share_result(supabase, row, covered, fields_for) -> int:
    """Give every sibling of `row` the outcome of this try. fields_for(sibling) -> the fields to write.
    The sibling ids are added to `covered` so that the caller skips them in the current round."""
    if covered is None:
        return 0
    siblings = sibling_rows(supabase, row)
    for sib in siblings:
        update_row(supabase, sib["id"], **fields_for(sib))
        covered.add(sib["id"])
    if siblings:
        print(f"  the same URL was queued {len(siblings)} more time(s): the result is shared with those rows.")
    return len(siblings)


def handle_row(supabase, row, capture, authenticate: bool = True, allow_fallback: bool = False, covered=None) -> str:
    """Try one URL. Returns done | retry | failed | rate_limited | unauthorized | auth_limited.

    auth_limited: the authenticated limit was hit and allow_fallback is set; the row is left untouched so the
    caller can try it again without the keys.
    covered: a set; the ids of the other due rows of the same URL that got this outcome are added to it.
    """
    url, attempts = row["url"], row["attempts"] + 1
    try:
        archive_url, _ = capture(url, authenticate=authenticate)
        print(f"Archived {url} -> {archive_url}" + ("" if authenticate else " (unauthenticated)"))
        update_row(supabase, row["id"], status="done", archive_url=archive_url, attempts=attempts, last_error=None)
        share_result(supabase, row, covered, lambda sib: {"status": "done", "archive_url": archive_url,
                                                           "attempts": sib["attempts"] + 1, "last_error": None})
        return "done"
    except TooManyRequests as e:
        if authenticate and allow_fallback:
            print(f"Rate limited with the S3 keys at {url}; switching to the unauthenticated mode.")
            return "auth_limited"
        print(f"Rate limited at {url}; stopping this run.")
        later = (now() + timedelta(minutes=RATE_LIMIT_MINUTES)).isoformat()
        update_row(supabase, row["id"], next_try_at=later, last_error=describe(e))
        share_result(supabase, row, covered, lambda sib: {"next_try_at": later, "last_error": describe(e)})
        return "rate_limited"
    except Unauthorized as e:
        # Bad / revoked keys: every URL would fail and burn its attempts. Keep the row untouched and stop.
        print(f"Internet Archive rejected the credentials ({describe(e)}); check IA_ACCESS_KEY / IA_SECRET_KEY.")
        return "unauthorized"
    except BlockedByRobots as e:
        print(f"Blocked by robots.txt: {url}")
        update_row(supabase, row["id"], status="failed", attempts=attempts, last_error=describe(e))
        share_result(supabase, row, covered, lambda sib: {"status": "failed", "attempts": sib["attempts"] + 1,
                                                           "last_error": describe(e)})
        return "failed"
    except Exception as e:
        print(f"Error archiving {url}: {describe(e)}")
        fields = retry_fields(attempts, e)
        update_row(supabase, row["id"], **fields)
        share_result(supabase, row, covered, lambda sib: retry_fields(sib["attempts"] + 1, e))
        return "failed" if fields.get("status") == "failed" else "retry"


def drain(supabase, capture, sleep=time.sleep, monotonic=time.monotonic,
          budget_sec: float = RUNTIME_MINUTES * 60, interval: float = INTERVAL_SEC, batch: int = BATCH_SIZE,
          anon_fallback: bool = ANON_FALLBACK, anon_interval: float = ANON_INTERVAL_SEC,
          auth_retry_sec: float = AUTH_RETRY_MINUTES * 60) -> dict:
    """Work through due rows round after round. Returns counters (plus 'stopped': why it ended)."""
    start = monotonic()
    counts = {"done": 0, "retry": 0, "failed": 0, "rate_limited": 0, "unauthorized": 0,
              "anonymous": 0, "switched": 0, "shared": 0}   # anonymous: archived without the keys; switched: times the mode changed; shared: rows resolved by another row of the same URL
    stopped = "queue empty"
    first = True
    anonymous = False       # True while the keys are rate limited
    auth_retry_at = 0.0     # monotonic time at which the keys are tried again
    while True:
        rows = due_rows(supabase, batch)
        if not rows:
            break
        print(f"{len(rows)} due URL(s) in this round" + (" (unauthenticated mode)." if anonymous else "."))
        halt = False
        covered = set()     # ids of rows that got the result of an earlier row (same URL) in this round
        for row in rows:
            if row["id"] in covered:
                counts["shared"] += 1
                continue
            if not first:
                sleep(anon_interval if anonymous else interval)
            if monotonic() - start >= budget_sec:
                stopped, halt = "time budget used up", True
                break
            first = False
            if anonymous and monotonic() >= auth_retry_at:
                anonymous = False
                counts["switched"] += 1
                print("Trying the S3 keys again.")
            result = handle_row(supabase, row, capture, authenticate=not anonymous,
                                allow_fallback=anon_fallback and not anonymous, covered=covered)
            if result == "auth_limited":
                anonymous = True
                auth_retry_at = monotonic() + auth_retry_sec
                counts["switched"] += 1
                result = handle_row(supabase, row, capture, authenticate=False, covered=covered)   # the same URL, without the keys
            counts[result] += 1
            if anonymous and result == "done":
                counts["anonymous"] += 1
            if result in ("rate_limited", "unauthorized"):
                stopped, halt = ("Internet Archive rate limit" if result == "rate_limited" else "credentials rejected"), True
                break
        if halt:
            break
    counts["stopped"] = stopped
    return counts


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
    print(f"Working for up to {RUNTIME_MINUTES:g} min (interval {INTERVAL_SEC}s, {BATCH_SIZE} per round; "
          + (f"without the keys after a rate limit: every {ANON_INTERVAL_SEC}s, keys retried after {AUTH_RETRY_MINUTES:g} min)."
             if ANON_FALLBACK else "stops at the first rate limit)."))
    counts = drain(supabase, savepagenow.capture_or_cache)
    remaining = len(due_rows(supabase, 1000))
    print(f"Finished ({counts['stopped']}): archived={counts['done']} will-retry={counts['retry']} "
          f"failed={counts['failed']} rate-limited={counts['rate_limited']} "
          f"unauthenticated={counts['anonymous']} shared-with-same-URL={counts['shared']}; still due now: {remaining}.")
    if counts["unauthorized"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
