#!/usr/bin/env bash
# Drain archive_queue from a local Linux machine (no GitHub Actions minutes used).
#
#   export SUPABASE_URL=https://<REF>.supabase.co
#   export SUPABASE_KEY=sb_secret_...
#   export IA_ACCESS_KEY=...
#   export IA_SECRET_KEY=...
#   bash scripts/drain-archive-local.sh
#
# Optional: ARCHIVE_INTERVAL_SEC (default 15), ARCHIVE_RUNTIME_MIN per round (default 720),
#           MAX_ROUNDS (default 20). Stop with Ctrl-C at any time; rows are saved one by one.
set -u
cd "$(dirname "$0")/.."

for v in SUPABASE_URL SUPABASE_KEY IA_ACCESS_KEY IA_SECRET_KEY; do
  if [ -z "${!v:-}" ]; then echo "Missing environment variable: $v" >&2; exit 1; fi
done

if [ ! -d .venv-archive ]; then
  python3 -m venv .venv-archive || exit 1
  .venv-archive/bin/pip install --quiet -r requirements.txt || exit 1
fi

export ARCHIVE_INTERVAL_SEC="${ARCHIVE_INTERVAL_SEC:-15}"
export ARCHIVE_RUNTIME_MIN="${ARCHIVE_RUNTIME_MIN:-720}"
export ARCHIVE_BATCH_SIZE="${ARCHIVE_BATCH_SIZE:-20}"
max_rounds="${MAX_ROUNDS:-20}"

for round in $(seq 1 "$max_rounds"); do
  echo "=== round $round/$max_rounds ($(date '+%F %T')) ==="
  log="$(mktemp)"
  .venv-archive/bin/python scripts/archive_worker.py 2>&1 | tee "$log"
  status="${PIPESTATUS[0]}"
  if [ "$status" -ne 0 ]; then
    echo "archive_worker failed (exit $status), e.g. invalid IA keys. Stopping." >&2
    rm -f "$log"; exit "$status"
  fi
  due="$(sed -n 's/.*still due now: \([0-9]*\)\..*/\1/p' "$log" | tail -n1)"
  rm -f "$log"
  if [ "${due:-0}" = "0" ]; then
    echo "Nothing is due any more. Rows that failed are rescheduled (30 min x 2^n); run again later for those."
    exit 0
  fi
  echo "Still due: $due. Waiting 15 min (rate limit) before the next round..."
  sleep 900
done
echo "Reached MAX_ROUNDS=$max_rounds."
