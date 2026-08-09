"""
The "remote UI" the Windows pipeline talks to: a prompt-submission page plus
the job-queue API pipeline.py polls and uploads results to.

Contract (must match pipeline.py / config.json's fetch_prompt_endpoint /
upload_endpoint exactly):
  GET  /jobs/next               -> 200 {"job_id": str, "prompt": str}, or 204 if none queued
  POST /jobs/<job_id>/result    -> multipart field "video", 200 on success
  POST /jobs/<job_id>/fail      -> JSON {"error": str}, 200 on success

Auth: a single shared secret (API_KEY env var). Browser routes use HTTP
Basic (any username, password = API_KEY); the three pipeline-facing routes
above use an `X-API-Key` header. Without this, anyone who finds the URL
could queue unlimited GPU jobs or view other people's generated videos.

Self-healing: a job claimed via /jobs/next but never completed or failed
(worker crashed, power loss, network partition - anything that skips the
explicit /fail call) is automatically treated as available again after
STALE_CLAIM_TIMEOUT_MINUTES, so it gets retried without manual intervention.
"""

import os
import sqlite3
import uuid
from functools import wraps
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template_string, request, send_from_directory, url_for

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
VIDEOS_DIR = DATA_DIR / "videos"
DB_PATH = DATA_DIR / "jobs.db"
DATA_DIR.mkdir(exist_ok=True)
VIDEOS_DIR.mkdir(exist_ok=True)

API_KEY = os.environ.get("API_KEY")
if not API_KEY:
    raise SystemExit("Set the API_KEY environment variable before starting the server (see server/README.md).")

STALE_CLAIM_TIMEOUT_MINUTES = int(os.environ.get("STALE_CLAIM_TIMEOUT_MINUTES", "30"))
MAX_JOB_RETRIES = int(os.environ.get("MAX_JOB_RETRIES", "3"))

app = Flask(__name__)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                prompt TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                claimed_at TEXT,
                done_at TEXT,
                video_filename TEXT,
                error_message TEXT,
                claim_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )


init_db()


def require_api_key(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if request.headers.get("X-API-Key") != API_KEY:
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)

    return wrapper


def require_browser_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.password != API_KEY:
            return Response("Auth required", 401, {"WWW-Authenticate": 'Basic realm="video-gen"'})
        return fn(*args, **kwargs)

    return wrapper


PAGE = """
<!doctype html>
<title>video-gen</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; }
  textarea { width: 100%; box-sizing: border-box; }
  table { border-collapse: collapse; width: 100%; margin-top: 1rem; }
  td, th { border: 1px solid #ccc; padding: 0.5rem; text-align: left; vertical-align: top; }
  video { max-width: 240px; }
  .status-failed { color: #b00020; }
  .status-in_progress { color: #9a6700; }
  .status-done { color: #1a7f37; }
  .error { font-size: 0.85em; color: #b00020; }
</style>
<h1>MiniMax H3 video generation</h1>
<form method="post" action="{{ url_for('submit_job') }}">
  <textarea name="prompt" rows="6" placeholder="Describe the video..." required></textarea><br>
  <button type="submit">Generate</button>
</form>
<h2>Jobs</h2>
<table>
<tr><th>ID</th><th>Prompt</th><th>Status</th><th>Attempts</th><th>Created</th><th>Result</th></tr>
{% for job in jobs %}
<tr>
  <td>{{ job.id[:8] }}</td>
  <td>{{ job.prompt[:200] }}
    {% if job.error_message %}<div class="error">{{ job.error_message[:200] }}</div>{% endif %}
  </td>
  <td class="status-{{ job.status }}">{{ job.status }}</td>
  <td>{{ job.claim_count }}</td>
  <td>{{ job.created_at }}</td>
  <td>{% if job.status == 'done' %}<video src="{{ url_for('get_video', job_id=job.id) }}" controls></video>{% endif %}</td>
</tr>
{% endfor %}
</table>
"""


@app.route("/")
@require_browser_auth
def index():
    with get_db() as conn:
        jobs = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 50").fetchall()
    return render_template_string(PAGE, jobs=jobs)


@app.route("/jobs", methods=["POST"])
@require_browser_auth
def submit_job():
    prompt = request.form.get("prompt") or (request.get_json(silent=True) or {}).get("prompt")
    if not prompt:
        return jsonify({"error": "prompt required"}), 400
    job_id = uuid.uuid4().hex
    with get_db() as conn:
        conn.execute("INSERT INTO jobs (id, prompt) VALUES (?, ?)", (job_id, prompt))
    if request.is_json:
        return jsonify({"job_id": job_id}), 201
    return redirect(url_for("index"))


@app.route("/jobs/next")
@require_api_key
def next_job():
    stale_cutoff = f"-{STALE_CLAIM_TIMEOUT_MINUTES} minutes"
    with get_db() as conn:
        # Stale claims (worker crashed/unreachable, never reported success or
        # failure) that have also exhausted retries: stop retrying, surface
        # as failed instead of stuck "in_progress" forever.
        conn.execute(
            """
            UPDATE jobs SET status='failed', done_at=datetime('now'),
                   error_message=COALESCE(error_message, 'worker crashed or unreachable, retries exhausted')
            WHERE status='in_progress' AND claimed_at < datetime('now', ?) AND claim_count >= ?
            """,
            (stale_cutoff, MAX_JOB_RETRIES),
        )

        # Real queued jobs first; stale claims still under the retry cap become
        # eligible again, so a crashed worker's job gets retried automatically.
        row = conn.execute(
            """
            SELECT * FROM jobs
            WHERE status='queued'
               OR (status='in_progress' AND claimed_at < datetime('now', ?) AND claim_count < ?)
            ORDER BY CASE WHEN status='queued' THEN 0 ELSE 1 END, created_at
            LIMIT 1
            """,
            (stale_cutoff, MAX_JOB_RETRIES),
        ).fetchone()
        if row is None:
            return "", 204
        conn.execute(
            "UPDATE jobs SET status='in_progress', claimed_at=datetime('now'), claim_count=claim_count+1 WHERE id=?",
            (row["id"],),
        )
    return jsonify({"job_id": row["id"], "prompt": row["prompt"]})


@app.route("/jobs/<job_id>/result", methods=["POST"])
@require_api_key
def upload_result(job_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return jsonify({"error": "unknown job_id"}), 404

    video = request.files.get("video")
    if video is None:
        return jsonify({"error": "video file required"}), 400
    filename = f"{job_id}.mp4"
    video.save(VIDEOS_DIR / filename)

    with get_db() as conn:
        conn.execute(
            "UPDATE jobs SET status='done', done_at=datetime('now'), video_filename=?, error_message=NULL WHERE id=?",
            (filename, job_id),
        )
    return jsonify({"ok": True})


@app.route("/jobs/<job_id>/fail", methods=["POST"])
@require_api_key
def fail_job(job_id):
    """Called by the pipeline when a job errors out (bad prompt, ComfyUI OOM,
    transient network error, etc.). Re-queued for another automatic attempt
    while under MAX_JOB_RETRIES; only marked permanently 'failed' once that's
    exhausted, so a single real error doesn't need a human to retry it, but a
    fundamentally broken prompt doesn't retry forever either. Purely
    best-effort from the pipeline's side - even if this call never arrives
    (worker crashed before it could report), the stale-claim timeout in
    next_job() picks the job back up regardless."""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return jsonify({"error": "unknown job_id"}), 404
        error = (request.get_json(silent=True) or {}).get("error", "unknown error")
        if row["claim_count"] < MAX_JOB_RETRIES:
            conn.execute(
                "UPDATE jobs SET status='queued', claimed_at=NULL, error_message=? WHERE id=?",
                (error, job_id),
            )
        else:
            conn.execute(
                "UPDATE jobs SET status='failed', done_at=datetime('now'), error_message=? WHERE id=?",
                (error, job_id),
            )
    return jsonify({"ok": True})


@app.route("/jobs/<job_id>")
@require_browser_auth
def job_status(job_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return jsonify({"error": "unknown job_id"}), 404
    return jsonify(dict(row))


@app.route("/videos/<job_id>.mp4")
@require_browser_auth
def get_video(job_id):
    return send_from_directory(VIDEOS_DIR, f"{job_id}.mp4")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
