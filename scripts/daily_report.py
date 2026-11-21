"""Daily report of the Cloud Run jobs, sent by e-mail (container mode `report`, Cloud Scheduler about 8:00 JST).

Reads the last REPORT_HOURS (default 24) hours: the executions of the jobs (Cloud Run Admin API), their logs and the
errors of Cloud Scheduler (Cloud Logging API), and the age of feeds/status.json. Nothing is written anywhere except the mail.

Also the size of the Artifact Registry repositories (free tier: 0.5 GiB; above it the report says "要確認"), because image
builds pile up there (2026-11-12: 3.9 GB). Needs the role artifactregistry.reader for it.

Needs the roles run.viewer and logging.viewer for the job's service account, and
  GMAIL_USER (the SMTP login = the Google account the app password belongs to), GMAIL_APP_PASSWORD (Gmail app password),
  REPORT_FROM (shown sender, default GMAIL_USER; an address of the same account, e.g. a +alias), REPORT_TO (default REPORT_FROM).
  Options (environment): GCP_PROJECT (gi-i-it), GCP_REGION (asia-northeast1), REPORT_JOBS (comma separated),
  REPORT_HOURS, SITE_BASE_URL, ARTIFACT_REPOS (comma separated, default "git"), ARTIFACT_FREE_GIB (default 0.5).   --dry-run prints the mail instead of sending it.
"""
import argparse
import os
import re
import smtplib
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import google.auth
import requests
from google.auth.transport.requests import AuthorizedSession

JST = timezone(timedelta(hours=9))
EXEC_LABEL = "run.googleapis.com/execution_name"
RE_SUMMARIZED = re.compile(r"^Summarized \S+ \(")
RE_GEMINI = re.compile(r"Error during Gemini API call \(([^,]+), attempt \d+\): (\d{3})")
RE_PAGES = re.compile(r"Diff pages written from their patches: (\d+)")
RE_EXIT = re.compile(r"Container called exit\((\d+)\)")
# website-stalker: "ERROR: <url> <reason>" (a site it could not fetch, e.g. "the server answered HTTP 202 Accepted instead of 200 OK");
# the pipeline's own guard: "<slug>: the server answers HTTP 202 instead of the page: not recorded"
RE_FETCH_ERROR = re.compile(r"ERROR:\s+(https?://\S+)\s+(.+)")
RE_GUARD = re.compile(r"^(\S+): the server answers (.+?) instead of the page: not recorded")
FETCH_FAIL_PROBLEM = int(os.environ.get("REPORT_FETCH_FAIL_MIN", "3"))


def parse_time(text):
    """RFC 3339 with up to nanoseconds -> aware datetime (None for empty)."""
    if not text:
        return None
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z", text)
    if not m:
        return None
    dt = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    if m.group(2):
        dt += timedelta(microseconds=int(float(m.group(2)) * 1_000_000))
    return dt


def jst(dt):
    return dt.astimezone(JST).strftime("%m/%d %H:%M") if dt else "-"


def duration(start, end):
    if not start or not end:
        return "-"
    sec = int((end - start).total_seconds())
    return f"{sec // 60}分{sec % 60:02d}秒"


def list_executions(session, project, region, job, since):
    url = f"https://run.googleapis.com/v2/projects/{project}/locations/{region}/jobs/{job}/executions"
    res = session.get(url, params={"pageSize": 50})
    if res.status_code == 404:
        return []
    res.raise_for_status()
    found = []
    for ex in res.json().get("executions", []):
        created = parse_time(ex.get("createTime"))
        if created and created >= since:
            found.append({
                "name": ex["name"].rsplit("/", 1)[-1], "job": job, "created": created,
                "start": parse_time(ex.get("startTime")), "end": parse_time(ex.get("completionTime")),
                "ok": int(ex.get("succeededCount", 0) or 0), "failed": int(ex.get("failedCount", 0) or 0),
                "running": int(ex.get("runningCount", 0) or 0), "cancelled": int(ex.get("cancelledCount", 0) or 0),
            })
    return sorted(found, key=lambda e: e["created"])


def read_logs(session, project, log_filter, max_pages=30):
    """All entries of the filter (oldest first), as dicts."""
    entries, token = [], None
    for _ in range(max_pages):
        body = {"resourceNames": [f"projects/{project}"], "filter": log_filter, "orderBy": "timestamp asc", "pageSize": 1000}
        if token:
            body["pageToken"] = token
        res = session.post("https://logging.googleapis.com/v2/entries:list", json=body)
        res.raise_for_status()
        data = res.json()
        entries += data.get("entries", [])
        token = data.get("nextPageToken")
        if not token:
            break
    return entries


def analyze_execution_logs(entries):
    """execution name -> counters from the lines of its log."""
    per = defaultdict(lambda: {"summaries": 0, "gemini": defaultdict(lambda: defaultdict(int)), "pages": None,
                               "exit": None, "oom": False, "skipped_deploy": False, "problems": [], "fetch_failed": {}})
    for e in entries:
        name = (e.get("labels") or {}).get(EXEC_LABEL)
        text = e.get("textPayload") or ""
        if not name or not text:
            continue
        info = per[name]
        for line in text.splitlines():
            m = RE_FETCH_ERROR.search(line)
            if m:
                key, reason = m.group(1), m.group(2).strip()
            else:
                m = RE_GUARD.match(line.strip())
                if not m:
                    continue
                key, reason = m.group(1), m.group(2)
            # one failure per site and run (the same fetch can be logged by both the fetcher and the pipeline guard)
            info["fetch_failed"].setdefault(key, (parse_time(e.get("timestamp")), reason))
        if RE_SUMMARIZED.match(text):
            info["summaries"] += 1
        m = RE_GEMINI.search(text)
        if m:
            info["gemini"][m.group(1)][m.group(2)] += 1
        m = RE_PAGES.search(text)
        if m:
            info["pages"] = int(m.group(1))
        m = RE_EXIT.search(text)
        if m:
            info["exit"] = int(m.group(1))
        if "Out-of-memory event" in text:
            info["oom"] = True
        if text.startswith("Summary of") and "failed" in text and len(info["problems"]) < 5:
            info["problems"].append(text[:160])
    return per


def scheduler_errors(session, project, since):
    flt = ('resource.type="cloud_scheduler_job" AND severity>=ERROR '
           'AND jsonPayload.@type="type.googleapis.com/google.cloud.scheduler.logging.AttemptFinished" '
           f'AND timestamp>="{since.strftime("%Y-%m-%dT%H:%M:%SZ")}"')
    rows = []
    for e in read_logs(session, project, flt, max_pages=3):
        payload = e.get("jsonPayload") or {}
        job = (e.get("resource", {}).get("labels") or {}).get("job_id", "?")
        rows.append((parse_time(e.get("timestamp")), job, payload.get("status") or payload.get("debugInfo") or "ERROR"))
    return rows


def fetch_failure_lines(per, stalk_names):
    """([lines for the mail], [problems]) for the sites a run could not fetch (HTTP 202, empty body, timeouts ...).

    The stored page stays as it is in such a run and the job still ends with status 0, so only the log shows it.
    A site that failed in at least FETCH_FAIL_PROBLEM runs of the period is a problem (a one-off failure is only listed)."""
    sites = defaultdict(list)  # key -> [(time, reason)]
    for name in stalk_names:
        for key, (when, reason) in (per[name]["fetch_failed"] if name in per else {}).items():
            sites[key].append((when, reason))
    lines, problems = [], []
    for key, hits in sorted(sites.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        hits.sort(key=lambda h: h[0] or datetime.min.replace(tzinfo=timezone.utc))
        reason = hits[-1][1]
        reason = reason if len(reason) <= 90 else reason[:87] + "..."
        times = "、".join(jst(w) for w, _ in hits[:6]) + ("、…" if len(hits) > 6 else "")
        lines.append(f"  {key}  {len(hits)} 回（{times}）: {reason}")
        if len(hits) >= FETCH_FAIL_PROBLEM:
            problems.append(f"{key} が {len(hits)} 回続けて取得できませんでした（{reason}）。保存済みのページは変わっていません。")
    return lines, problems


GIB = 1024 ** 3


def repository_sizes(session, project, region, repos):
    """[(repository, size in bytes or None, error text)] from the Artifact Registry API."""
    rows = []
    for repo in repos:
        url = f"https://artifactregistry.googleapis.com/v1/projects/{project}/locations/{region}/repositories/{repo}"
        try:
            res = session.get(url, timeout=30)
            if res.status_code != 200:
                rows.append((repo, None, f"HTTP {res.status_code}"))
                continue
            rows.append((repo, int(res.json().get("sizeBytes") or 0), ""))
        except (requests.RequestException, ValueError) as e:
            rows.append((repo, None, type(e).__name__))
    return rows


def artifact_lines(rows, free_gib):
    """(lines for the mail, problems): a repository above the free tier, or one that could not be read, is a problem."""
    lines, problems = [], []
    free = free_gib * GIB
    for repo, size, error in rows:
        if size is None:
            lines.append(f"  {repo}: 取得できませんでした（{error}）")
            problems.append(f"Artifact Registry の {repo} のサイズを取得できませんでした（{error}。artifactregistry.reader が必要です）。")
            continue
        lines.append(f"  {repo}: {size / GIB:.2f} GiB / 無料枠 {free_gib:g} GiB（{size / free * 100:.0f}%）")
        if size > free:
            problems.append(f"Artifact Registry の {repo} が無料枠を超えています（{size / GIB:.2f} GiB、無料枠 {free_gib:g} GiB）。"
                            "古いイメージの削除ポリシーを確認してください。")
    return lines, problems


def status_generated(base):
    if not base:
        return None
    try:
        res = requests.get(base.rstrip("/") + "/feeds/status.json", timeout=20, headers={"Cache-Control": "no-cache"})
        res.raise_for_status()
        return parse_time(res.json().get("generated"))
    except Exception:
        return None


def make_session():
    """Application default credentials (the job's service account); on a computer without them, the token of `gcloud auth`."""
    try:
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        return AuthorizedSession(creds)
    except google.auth.exceptions.DefaultCredentialsError:
        import shutil
        import subprocess
        gcloud = shutil.which("gcloud") or shutil.which("gcloud.cmd")
        token = subprocess.run([gcloud, "auth", "print-access-token"], capture_output=True, text=True, check=True).stdout.strip()
        session = requests.Session()
        session.headers["Authorization"] = f"Bearer {token}"
        session.headers["x-goog-user-project"] = os.environ.get("GCP_PROJECT", "gi-i-it")
        return session


def build_report(hours):
    project = os.environ.get("GCP_PROJECT", "gi-i-it")
    region = os.environ.get("GCP_REGION", "asia-northeast1")
    jobs = [j.strip() for j in os.environ.get("REPORT_JOBS", "g-i-t-stalk,g-i-t-remake-dashboard,g-i-t-recover,g-i-t-news-morning").split(",") if j.strip()]
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    session = make_session()

    executions = []
    for job in jobs:
        executions += list_executions(session, project, region, job, since)
    executions.sort(key=lambda e: e["created"])

    log_filter = ('resource.type="cloud_run_job" AND (' + " OR ".join(f'resource.labels.job_name="{j}"' for j in jobs) + ') '
                  f'AND timestamp>="{since.strftime("%Y-%m-%dT%H:%M:%SZ")}"')
    per = analyze_execution_logs(read_logs(session, project, log_filter))
    sched = scheduler_errors(session, project, since)
    generated = status_generated(os.environ.get("SITE_BASE_URL", ""))
    repos = [r.strip() for r in os.environ.get("ARTIFACT_REPOS", "git").split(",") if r.strip()]
    try:
        free_gib = float(os.environ.get("ARTIFACT_FREE_GIB", "0.5"))
    except ValueError:
        free_gib = 0.5
    artifact_rows, artifact_problems = artifact_lines(repository_sizes(session, project, region, repos), free_gib)

    lines, problems = [], []
    stalk = [e for e in executions if e["job"] == "g-i-t-stalk"]
    for ex in executions:
        info = per.get(ex["name"])
        ok = ex["failed"] == 0 and ex["cancelled"] == 0 and ex["running"] == 0 and ex["ok"] > 0
        state = "成功" if ok else ("実行中" if ex["running"] else "失敗")
        row = f"{jst(ex['start'] or ex['created'])}  {ex['job']}  {state}  {duration(ex['start'], ex['end'])}"
        if info:
            if ex["job"] == "g-i-t-stalk":
                row += f"  更新 {info['pages'] if info['pages'] is not None else 0} 件・要約 {info['summaries']} 件"
            if info["oom"]:
                row += "  メモリ不足あり"
        lines.append(row)
        if not ok and state == "失敗":
            problems.append(f"{ex['job']} の実行 {ex['name']} が失敗しました（{jst(ex['start'] or ex['created'])}）。")
        if info and info["oom"]:
            problems.append(f"{ex['name']} でメモリ不足が出ました。")
        if info and info["problems"]:
            lines.append("    要約の失敗: " + " / ".join(info["problems"][:2]))
    if not executions:
        lines.append("（この期間に実行がありません）")
        problems.append("この期間に Cloud Run ジョブの実行がありません。")

    gemini = defaultdict(lambda: defaultdict(int))
    for info in per.values():
        for model, codes in info["gemini"].items():
            for code, n in codes.items():
                gemini[model][code] += n
    gemini_lines = [f"  {model}: " + "、".join(f"{code} が {n} 回" for code, n in sorted(codes.items())) for model, codes in sorted(gemini.items())]

    sched_lines = [f"  {jst(t)}  {job}  {status}" for t, job, status in sched]
    if sched:
        problems.append(f"Cloud Scheduler の起動の試行が {len(sched)} 回失敗しました（再試行で復旧した可能性があります）。")

    if generated:
        age = now - generated
        gen_line = f"status.json の更新: {jst(generated)}（{int(age.total_seconds() // 3600)} 時間前）"
        if age > timedelta(hours=17):
            problems.append("status.json が 17 時間以上更新されていません。")
    else:
        gen_line = "status.json の更新: 取得できませんでした"

    fetch_lines, fetch_problems = fetch_failure_lines(per, [e["name"] for e in stalk])
    problems += fetch_problems
    problems += artifact_problems
    total_updates = sum((per[e["name"]]["pages"] or 0) for e in stalk if e["name"] in per)
    if problems:
        subject = f"[G醫t] 要確認（{len(problems)} 件）"
    else:
        subject = f"[G醫t] 正常（実行 {len(stalk)} 回、更新 {total_updates} 件）"

    body = [f"{jst(since)} から {jst(now)} JST までの記録です。", ""]
    if problems:
        body += ["■ 要確認"] + [f"  ・{p}" for p in problems] + [""]
    body += ["■ 実行"] + [f"  {l}" if not l.startswith("    ") else l for l in lines] + [""]
    if fetch_lines:
        body += ["■ 取得できなかったサイト（保存済みのページは変えていません）"] + fetch_lines + [""]
    if gemini_lines:
        body += ["■ Gemini のエラー（再試行・別モデルへの切り替えを含む）"] + gemini_lines + [""]
    if sched_lines:
        body += ["■ Cloud Scheduler の失敗"] + sched_lines + [""]
    if artifact_rows:
        body += ["■ Artifact Registry の容量"] + artifact_rows + [""]
    body += [gen_line]
    base = os.environ.get("SITE_BASE_URL", "")
    if base:
        body += [f"サイト: {base}"]
    body += ["", f"ログ: https://console.cloud.google.com/run/jobs?project={project}"]
    return subject, "\n".join(body)


def send(subject, text):
    user, password = os.environ["GMAIL_USER"], os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
    msg = EmailMessage()
    sender = os.environ.get("REPORT_FROM") or user
    msg["Subject"], msg["From"], msg["To"] = subject, sender, os.environ.get("REPORT_TO") or sender
    msg.set_content(text)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="print the mail, do not send it")
    p.add_argument("--hours", type=int, default=int(os.environ.get("REPORT_HOURS", "24")))
    args = p.parse_args()
    subject, text = build_report(args.hours)
    if args.dry_run:
        print("Subject:", subject)
        print()
        print(text)
        return 0
    for v in ("GMAIL_USER", "GMAIL_APP_PASSWORD"):
        if not os.environ.get(v):
            print(f"Missing environment variable: {v}", file=sys.stderr)
            return 1
    send(subject, text)
    print(f"Sent: {subject}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
