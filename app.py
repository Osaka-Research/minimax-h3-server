"""
The "remote UI" shared by multiple generation pipelines (Windows video via
minimax-h3-windows, image via nano-banana-windows, etc): a prompt-submission
page plus the job-queue API each pipeline polls and uploads results to.
Jobs carry a `type` (e.g. "video", "image") so each pipeline only claims
work it can actually do.

Contract (must match each pipeline's config.json fetch_prompt_endpoint /
upload_endpoint exactly):
  GET  /jobs/next?type=<type>   -> 200 {"job_id": str, "prompt": str}, or 204 if none queued
                                    (type defaults to "video" if omitted, for
                                    pipelines that predate multi-type support)
  POST /jobs/<job_id>/result    -> multipart field "video" or "image" (whichever
                                    matches the job's type), 200 on success
  POST /jobs/<job_id>/fail      -> JSON {"error": str}, 200 on success

Auth: none. Every route, including the pipeline-facing ones, is open to
anyone with this URL - that means anyone who has it can queue generation
jobs your machines will process, or claim/complete jobs as if they were
your own pipeline. Deliberate tradeoff for this deployment, not an
oversight.

Self-healing: a job claimed via /jobs/next but never completed or failed
(worker crashed, power loss, network partition - anything that skips the
explicit /fail call) is automatically treated as available again after
STALE_CLAIM_TIMEOUT_MINUTES, so it gets retried without manual intervention.
"""

import os
import sqlite3
import uuid
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template_string, request, send_from_directory, url_for

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MEDIA_DIR = DATA_DIR / "media"
DB_PATH = DATA_DIR / "jobs.db"
DATA_DIR.mkdir(exist_ok=True)
MEDIA_DIR.mkdir(exist_ok=True)

VALID_TYPES = {"video", "image"}
# field name the pipeline uploads its result under, and the extension we
# store it with, per job type
RESULT_FIELD_BY_TYPE = {"video": ("video", "mp4"), "image": ("image", "png")}

STALE_CLAIM_TIMEOUT_MINUTES = int(os.environ.get("STALE_CLAIM_TIMEOUT_MINUTES", "30"))
MAX_JOB_RETRIES = int(os.environ.get("MAX_JOB_RETRIES", "3"))

app = Flask(__name__)


def get_db():
    # Longer than the 5s default: with several devices polling concurrently
    # under gunicorn (multiple worker processes), a request may need to wait
    # for another's write transaction rather than fail outright.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                prompt TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'video',
                status TEXT NOT NULL DEFAULT 'queued',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                claimed_at TEXT,
                done_at TEXT,
                media_filename TEXT,
                error_message TEXT,
                claim_count INTEGER NOT NULL DEFAULT 0,
                claimed_by TEXT
            )
            """
        )
        # CREATE TABLE IF NOT EXISTS won't add new columns to an existing DB
        # file from an older version of this server - migrate it if needed.
        existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "claimed_by" not in existing_columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN claimed_by TEXT")
        if "type" not in existing_columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN type TEXT NOT NULL DEFAULT 'video'")
        if "media_filename" not in existing_columns:
            if "video_filename" in existing_columns:
                # Older DB from before multi-type support - keep existing
                # video filenames intact under the new generalized column name.
                conn.execute("ALTER TABLE jobs ADD COLUMN media_filename TEXT")
                conn.execute("UPDATE jobs SET media_filename = video_filename")
            else:
                conn.execute("ALTER TABLE jobs ADD COLUMN media_filename TEXT")


init_db()


PAGE = """
<!doctype html>
<title>generation queue</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; }
  textarea { width: 100%; box-sizing: border-box; }
  table { border-collapse: collapse; width: 100%; margin-top: 1rem; }
  td, th { border: 1px solid #ccc; padding: 0.5rem; text-align: left; vertical-align: top; }
  video, img.result { max-width: 240px; }
  .status-failed { color: #b00020; }
  .status-in_progress { color: #9a6700; }
  .status-done { color: #1a7f37; }
  .error { font-size: 0.85em; color: #b00020; }
</style>
<h1>Generation queue</h1>
<form method="post" action="{{ url_for('submit_job') }}">
  <textarea name="prompt" rows="6" placeholder="Describe what to generate..." required></textarea><br>
  <select name="type">
    <option value="video">video (MiniMax H3)</option>
    <option value="image">image (Nano Banana)</option>
  </select>
  <button type="submit">Generate</button>
</form>
<h2>Jobs</h2>
<table>
<tr><th>ID</th><th>Type</th><th>Prompt</th><th>Status</th><th>Device</th><th>Attempts</th><th>Created</th><th>Result</th></tr>
{% for job in jobs %}
<tr>
  <td>{{ job.id[:8] }}</td>
  <td>{{ job.type }}</td>
  <td>{{ job.prompt[:200] }}
    {% if job.error_message %}<div class="error">{{ job.error_message[:200] }}</div>{% endif %}
  </td>
  <td class="status-{{ job.status }}">{{ job.status }}</td>
  <td>{{ job.claimed_by or '' }}</td>
  <td>{{ job.claim_count }}</td>
  <td>{{ job.created_at }}</td>
  <td>
    {% if job.status == 'done' and job.type == 'video' %}
      <video src="{{ url_for('get_media', job_id=job.id) }}" controls></video>
    {% elif job.status == 'done' and job.type == 'image' %}
      <img class="result" src="{{ url_for('get_media', job_id=job.id) }}">
    {% endif %}
  </td>
</tr>
{% endfor %}
</table>
"""


@app.route("/")
def index():
    with get_db() as conn:
        jobs = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 50").fetchall()
    return render_template_string(PAGE, jobs=jobs)


@app.route("/jobs", methods=["POST"])
def submit_job():
    body = request.get_json(silent=True) or {}
    prompt = request.form.get("prompt") or body.get("prompt")
    if not prompt:
        return jsonify({"error": "prompt required"}), 400
    job_type = request.form.get("type") or body.get("type") or "video"
    if job_type not in VALID_TYPES:
        return jsonify({"error": f"type must be one of {sorted(VALID_TYPES)}"}), 400
    job_id = uuid.uuid4().hex
    with get_db() as conn:
        conn.execute("INSERT INTO jobs (id, prompt, type) VALUES (?, ?, ?)", (job_id, prompt, job_type))
    if request.is_json:
        return jsonify({"job_id": job_id}), 201
    return redirect(url_for("index"))


@app.route("/jobs/next")
def next_job():
    """Multiple devices can poll this concurrently - there's no device
    registration or routing, it's first-request-wins. The claim itself has
    to be race-safe: a plain SELECT-candidate-then-UPDATE-by-id (the
    previous approach here) lets two concurrent requests both select the
    same row before either UPDATE commits, handing the same job to two
    devices at once. Fixed by re-checking the expected pre-claim state
    inside the UPDATE's WHERE clause and retrying on the rare row that
    another request claims first (rowcount == 0)."""
    worker_id = request.headers.get("X-Worker-Id", "unknown")
    job_type = request.args.get("type", "video")
    if job_type not in VALID_TYPES:
        return jsonify({"error": f"type must be one of {sorted(VALID_TYPES)}"}), 400
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

        for _ in range(5):
            # Real queued jobs first; stale claims still under the retry cap
            # become eligible again, so a crashed worker's job gets retried.
            row = conn.execute(
                """
                SELECT * FROM jobs
                WHERE type=? AND (
                    status='queued'
                    OR (status='in_progress' AND claimed_at < datetime('now', ?) AND claim_count < ?)
                )
                ORDER BY CASE WHEN status='queued' THEN 0 ELSE 1 END, created_at
                LIMIT 1
                """,
                (job_type, stale_cutoff, MAX_JOB_RETRIES),
            ).fetchone()
            if row is None:
                return "", 204

            # Only succeeds if the row is still in the state we just saw it
            # in - if another concurrent request already claimed it, this
            # matches zero rows and we loop to pick a different candidate.
            cur = conn.execute(
                """
                UPDATE jobs SET status='in_progress', claimed_at=datetime('now'),
                       claim_count=claim_count+1, claimed_by=?
                WHERE id=? AND type=? AND (
                    status='queued'
                    OR (status='in_progress' AND claimed_at < datetime('now', ?) AND claim_count < ?)
                )
                """,
                (worker_id, row["id"], job_type, stale_cutoff, MAX_JOB_RETRIES),
            )
            if cur.rowcount == 1:
                return jsonify({"job_id": row["id"], "prompt": row["prompt"]})
            # else: lost the race for this row - loop and try the next candidate

    return "", 204


@app.route("/jobs/<job_id>/result", methods=["POST"])
def upload_result(job_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return jsonify({"error": "unknown job_id"}), 404

    field_name, ext = RESULT_FIELD_BY_TYPE[row["type"]]
    upload = request.files.get(field_name)
    if upload is None:
        return jsonify({"error": f"'{field_name}' file required for type '{row['type']}'"}), 400
    filename = f"{job_id}.{ext}"
    upload.save(MEDIA_DIR / filename)

    with get_db() as conn:
        conn.execute(
            "UPDATE jobs SET status='done', done_at=datetime('now'), media_filename=?, error_message=NULL WHERE id=?",
            (filename, job_id),
        )
    return jsonify({"ok": True})


@app.route("/jobs/<job_id>/fail", methods=["POST"])
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
def job_status(job_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return jsonify({"error": "unknown job_id"}), 404
    return jsonify(dict(row))


@app.route("/media/<job_id>")
def get_media(job_id):
    with get_db() as conn:
        row = conn.execute("SELECT media_filename FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None or row["media_filename"] is None:
        return jsonify({"error": "no result for this job"}), 404
    return send_from_directory(MEDIA_DIR, row["media_filename"])


@app.route("/healthz")
def healthz():
    # Deliberately no auth: Render's own health check hits this to decide
    # whether to route traffic here at all. If it hit an authenticated route
    # instead and got a 401, it could plausibly - and wrongly - treat this
    # instance as not ready, which would explain intermittent "no-server"
    # responses from Render's edge despite the app never actually crashing.
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
