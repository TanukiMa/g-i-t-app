"""Drain archive_queue: register pending URLs with the Internet Archive, one at a time.

GitHub only fires a "*/15" schedule a few times a day, so one run must not stop after a fixed handful of
URLs: it keeps working until nothing is due any more or the time budget is used up.

Levels: with an S3 key the Internet Archive allows a higher rate. One or more keys can be given (IA_ACCESS_KEY /
IA_SECRET_KEY, then IA_ACCESS_KEY_2 / IA_SECRET_KEY_2 ... up to _9). The worker uses the first key; when it is rate
limited (HTTP 429) it moves on to the second, then the third ..., and after the last key to the unauthenticated mode,
which is slow (ARCHIVE_ANON_INTERVAL_SEC). A key that was limited is tried again after ARCHIVE_AUTH_RETRY_MIN
minutes (the lowest-numbered usable key is always preferred). It stops on a 429 only when every level is limited
(the unauthenticated mode counts as the last level unless ARCHIVE_ANON_FALLBACK=0). A key that the Internet Archive
rejects (Unauthorized) is left out for the rest of the run and the run ends with exit status 1, so that it gets noticed.

The same URL can be queued several times (a page that was updated twice before the worker ran; one commit per update).
A URL is registered ONCE per run and the result is shared with every other due row of that URL (no row is deleted:
each commit keeps its own row and Wayback link). The rows that are not due yet, and rows that are already done, are
not touched: a later commit must not get an older capture.
"""
import os
import sys
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait as wait_for
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
INTERVAL_SEC = int(os.environ.get("ARCHIVE_INTERVAL_SEC", "10"))    # at least this long between the START of two captures (all workers together)
WORKERS = int(os.environ.get("ARCHIVE_WORKERS") or 3)               # captures running at the same time (1 = one after the other)
RUNTIME_MINUTES = float(os.environ.get("ARCHIVE_RUNTIME_MIN") or 14)  # stop starting new URLs after this
MAX_ATTEMPTS = 5
BACKOFF_MINUTES = 30      # doubled per failed attempt
RATE_LIMIT_MINUTES = 15   # retry delay after HTTP 429
# Unauthenticated fallback after the authenticated rate limit (0 = off: stop at the first 429 as before).
ANON_FALLBACK = os.environ.get("ARCHIVE_ANON_FALLBACK", "1") != "0"
ANON_INTERVAL_SEC = int(os.environ.get("ARCHIVE_ANON_INTERVAL_SEC") or 60)   # slower pace without keys
AUTH_RETRY_MINUTES = float(os.environ.get("ARCHIVE_AUTH_RETRY_MIN") or 15)   # then try the keys again


MAX_KEYS = 9


def now():
    return datetime.now(timezone.utc)


def load_keys(env=None) -> list:
    """[(access, secret), ...] from IA_ACCESS_KEY/IA_SECRET_KEY and IA_ACCESS_KEY_2/IA_SECRET_KEY_2 ... (_9). A pair of
    which only one half is set is reported and skipped."""
    env = os.environ if env is None else env
    keys = []
    for suffix in [""] + [f"_{n}" for n in range(2, MAX_KEYS + 1)]:
        access, secret = env.get("IA_ACCESS_KEY" + suffix), env.get("IA_SECRET_KEY" + suffix)
        if access and secret:
            keys.append((access, secret))
        elif access or secret:
            print(f"IA_ACCESS_KEY{suffix} and IA_SECRET_KEY{suffix} must be set together; this pair is ignored.")
    return keys


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


def rate_limit_row(supabase, row, covered, error):
    """Every level is limited: try the row (and the other rows of the same URL) again later; the attempt does not count."""
    later = (now() + timedelta(minutes=RATE_LIMIT_MINUTES)).isoformat()
    update_row(supabase, row["id"], next_try_at=later, last_error=describe(error))
    share_result(supabase, row, covered, lambda sib: {"next_try_at": later, "last_error": describe(error)})


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
            print(f"Rate limited with the S3 key at {url}; moving on to the next level.")
            return "auth_limited"
        print(f"Rate limited at {url}; stopping this run.")
        rate_limit_row(supabase, row, covered, e)
        return "rate_limited"
    except Unauthorized as e:
        # Bad / revoked key: every URL would fail and burn its attempts. Keep the row untouched; the caller leaves the key out.
        print(f"Internet Archive rejected the credentials ({describe(e)}); check IA_ACCESS_KEY / IA_SECRET_KEY (and the numbered ones).")
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
          auth_retry_sec: float = AUTH_RETRY_MINUTES * 60, n_keys: int = 1, use_key=None, workers: int = WORKERS) -> dict:
    """Work through due rows round after round. Returns counters (plus 'stopped': why it ended).

    Levels: 0 .. n_keys-1 are the S3 keys (use_key(i) makes key i the one that `capture` uses), the last level
    (index n_keys, only when anon_fallback) is the unauthenticated mode. A row is tried at the lowest level that is
    usable now; a 429 marks the level limited for auth_retry_sec and the same row goes on to the next level.

    Up to `workers` captures run at the same time (a capture takes 20-40 s, so one at a time left most of the
    allowed rate unused), but a new one is started at most every `interval` seconds (`anon_interval` without
    keys, and only one at a time there), so the rate stays under the Internet Archive's limit whatever the number of
    workers. The same URL is never registered twice at once (the second row waits for the result of the first and
    gets it as a sibling). The level only changes while nothing is in flight, because the key reaches the library
    through environment variables."""
    start = monotonic()
    counts = {"done": 0, "retry": 0, "failed": 0, "rate_limited": 0, "unauthorized": 0,
              "anonymous": 0, "switched": 0, "shared": 0}   # anonymous: archived without the keys; switched: times the level changed; shared: rows resolved by another row of the same URL
    levels = n_keys + (1 if anon_fallback else 0)
    blocked_until = [0.0] * levels      # monotonic time until which a level is not used (inf: rejected by the Internet Archive)
    per_level = [0] * levels            # URLs archived per level, for the log
    current = None                      # the level of the previous try
    stopped = "queue empty"
    last_start = None                   # when the last capture was started (pacing)
    workers = max(1, int(workers))

    def usable():
        now_ = monotonic()
        for i in range(levels):
            if blocked_until[i] <= now_:
                return i
        return None

    def name(level):
        return "unauthenticated mode" if level >= n_keys else f"S3 key {level + 1}"

    def run(row, level, covered):
        try:
            return handle_row(supabase, row, capture, authenticate=level < n_keys,
                              allow_fallback=level < levels - 1, covered=covered)
        except Exception as e:      # handle_row catches what the capture raises; this is for what it does not expect
            print(f"Unexpected error at {row['url']}: {describe(e)}")
            update_row(supabase, row["id"], **retry_fields(row["attempts"] + 1, e))
            return "retry"

    halt = False
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while not halt:
            rows = due_rows(supabase, batch)
            if not rows:
                break
            print(f"{len(rows)} due URL(s) in this round" + (f" ({name(current)})." if current is not None and current >= n_keys else "."))
            covered = set()         # ids of rows that got the result of an earlier row (same URL) in this round
            todo = deque(rows)      # not started yet
            retry_q = deque()       # rows that must go on to the next level
            deferred = []           # rows whose URL is being captured right now
            flying = {}             # future -> (row, level)

            while (todo or retry_q or deferred or flying) and not (halt and not flying):
                # 1. results that are in
                for fut in [f for f in flying if f.done()]:
                    row, level = flying.pop(fut)
                    result = fut.result()
                    if result == "auth_limited":
                        blocked_until[level] = monotonic() + auth_retry_sec
                        retry_q.appendleft(row)
                    elif result == "unauthorized":
                        counts["unauthorized"] += 1
                        blocked_until[level] = float("inf")
                        print(f"The {name(level)} is left out for the rest of this run.")
                        retry_q.appendleft(row)
                    elif result == "rate_limited":
                        counts["rate_limited"] += 1
                        stopped, halt = "Internet Archive rate limit", True
                    else:
                        counts[result] += 1
                        if result == "done":
                            per_level[level] += 1
                            if level >= n_keys:
                                counts["anonymous"] += 1
                    if deferred:    # their URL may have been finished (and shared) now
                        todo.extendleft(reversed(deferred))
                        deferred = []
                if halt:
                    continue

                # 2. the next row, if there is room for it
                cap = 1 if current is not None and current >= n_keys else workers
                if (todo or retry_q) and len(flying) < cap:
                    queue = retry_q if retry_q else todo
                    row = queue[0]
                    if row["id"] in covered:
                        queue.popleft()
                        counts["shared"] += 1
                        continue
                    if any(r["url"] == row["url"] for r, _ in flying.values()):
                        deferred.append(queue.popleft())
                        continue
                    if monotonic() - start >= budget_sec:
                        stopped, halt = "time budget used up", True
                        continue
                    level = usable()
                    if level is None:       # every level is limited or rejected
                        queue.popleft()
                        if not anon_fallback and all(b == float("inf") for b in blocked_until):
                            stopped, halt = "credentials rejected", True
                        else:
                            rate_limit_row(supabase, row, covered, "every level is rate limited")
                            counts["rate_limited"] += 1
                            stopped, halt = "Internet Archive rate limit", True
                        continue
                    if level != current and flying:
                        pass                # the level changes only when nothing is in flight: wait for the results below
                    else:
                        gap = anon_interval if level >= n_keys else interval
                        wait = 0 if last_start is None else last_start + gap - monotonic()
                        if wait > 0:
                            if flying:
                                wait_for(list(flying), timeout=min(wait, 1.0), return_when=FIRST_COMPLETED)
                            else:
                                sleep(wait)
                            continue
                        queue.popleft()
                        if level != current:
                            if current is not None:
                                counts["switched"] += 1
                                print(f"Now using the {name(level)}.")
                            current = level
                            if level < n_keys and use_key:
                                use_key(level)
                        last_start = monotonic()
                        flying[pool.submit(run, row, level, covered)] = (row, level)
                        continue
                # 3. nothing to start now: wait for a result
                if flying:
                    wait_for(list(flying), timeout=1.0, return_when=FIRST_COMPLETED)
                elif deferred and not (todo or retry_q):
                    todo.extend(deferred)   # nothing is in flight any more: they are due again
                    deferred = []
    counts["stopped"] = stopped
    counts["per_level"] = {name(i): per_level[i] for i in range(levels) if per_level[i]}
    return counts


def main():
    supa_url = os.environ.get("SUPABASE_URL")
    supa_key = os.environ.get("SUPABASE_KEY")
    keys = load_keys()
    if not (savepagenow and create_client and supa_url and supa_key and keys):
        print("Missing modules or credentials (SUPABASE_*, IA_*).")
        sys.exit(1)

    def use_key(index: int):
        # savepagenow reads its credentials from these env vars (at every call), not from arguments.
        os.environ["SAVEPAGENOW_ACCESS_KEY"], os.environ["SAVEPAGENOW_SECRET_KEY"] = keys[index]

    use_key(0)
    supabase = create_client(supa_url, supa_key)
    print(f"{len(keys)} S3 key(s). Working for up to {RUNTIME_MINUTES:g} min ({WORKERS} at a time, one started every {INTERVAL_SEC}s, {BATCH_SIZE} per round; "
          f"a rate-limited key is tried again after {AUTH_RETRY_MINUTES:g} min; "
          + (f"after the last key: unauthenticated, every {ANON_INTERVAL_SEC}s)." if ANON_FALLBACK else "after the last key the run stops)."))
    counts = drain(supabase, savepagenow.capture_or_cache, n_keys=len(keys), use_key=use_key, workers=WORKERS)
    remaining = len(due_rows(supabase, 1000))
    print(f"Finished ({counts['stopped']}): archived={counts['done']} will-retry={counts['retry']} "
          f"failed={counts['failed']} rate-limited={counts['rate_limited']} "
          f"unauthenticated={counts['anonymous']} shared-with-same-URL={counts['shared']} "
          f"level-changes={counts['switched']}; still due now: {remaining}.")
    if counts["per_level"]:
        print("Archived per level: " + ", ".join(f"{k}={v}" for k, v in counts["per_level"].items()))
    if counts["unauthorized"]:
        print(f"{counts['unauthorized']} key(s) were rejected by the Internet Archive: check the IA_* secrets.")
        sys.exit(1)


if __name__ == "__main__":
    main()
