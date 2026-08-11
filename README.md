# minimax-h3-server

Shared "remote UI" for multiple generation pipelines — currently
[`minimax-h3-windows`](https://github.com/Osaka-Research/minimax-h3-windows)
(video) and [`nano-banana-windows`](https://github.com/Osaka-Research/nano-banana-windows)
(image): a page to submit prompts, a type-routed job queue, and where
finished results land. Deploy this anywhere reachable over HTTPS (a VPS, a
hosting platform, a tunnel); one or more machines running either pipeline
poll it for work and upload results back.

- `GET /` — web page: pick a type (video/image), submit a prompt, see
  queued/in-progress/done jobs, view finished results inline. No login —
  see Auth below (applies to every route, not just this one).
- `GET /jobs/next?type=video|image` — polled by a pipeline; returns the
  oldest queued job of that type and marks it claimed, or 204 if none.
  `type` defaults to `video` if omitted, for pipelines that predate
  multi-type support. Race-safe under multiple concurrent devices polling
  at once (verified under `gunicorn -w 4` with 20 simultaneous requests
  against 5 jobs — each claimed exactly once).
- `POST /jobs/<job_id>/result` — multipart field `video` or `image`
  (whichever matches the job's type) — the pipeline uploads its finished
  result here.
- `POST /jobs/<job_id>/fail` — a pipeline reports a failed job; re-queued
  automatically up to `MAX_JOB_RETRIES` before being marked permanently
  failed.
- `GET /jobs/<job_id>` — JSON status for one job.
- `GET /media/<job_id>` — the stored result (video or image, whatever that
  job produced).

Jobs are stored in a local SQLite file (`data/jobs.db`); results in
`data/media/`. Both are gitignored - this is local state, not something to
commit.

## Auth

None. Every route, including the pipeline-facing ones (`/jobs/next`,
`/jobs/<id>/result`, `/jobs/<id>/fail`), is open to anyone with this URL.
That means anyone who has it can queue generation jobs your machines will
process, or claim/complete jobs as if they were your own pipeline. Deliberate
tradeoff, not an oversight — if that's not acceptable for your deployment,
add auth back in front of it (e.g. a reverse proxy with Basic Auth).

## Run locally

```bash
pip install -r requirements.txt
python app.py
```

Listens on `0.0.0.0:8000` (override with `PORT`).

For anything beyond local testing, run it behind a real WSGI server instead
of Flask's dev server:

```bash
gunicorn -w 2 -b 0.0.0.0:8000 app:app
```

Optional tuning env vars: `MAX_JOB_RETRIES` (default 3),
`STALE_CLAIM_TIMEOUT_MINUTES` (default 30, for reclaiming jobs whose worker
crashed without reporting failure).

## Deploying so the pipeline machine(s) can reach it

This needs to be reachable from wherever a pipeline runs - options include
a small VPS, a platform like Render/Railway/Fly.io, or a tunnel
(ngrok/Cloudflare Tunnel) to a machine on your own network. Any of these
work the same way: run the app (ideally via gunicorn) and get a public
HTTPS URL. Also set a health check path of `/healthz` if the
platform supports one and hits `/` by default - `/` is fine to load in a
browser but a health checker expecting a plain 2xx on `/` can behave
oddly depending on what's rendered there, `/healthz` is always a bare 200.

Once you have that URL, on each pipeline machine's `config.json`:

```jsonc
{
  "remote_ui_base_url": "https://your-deployed-url.example"
}
```

`fetch_prompt_endpoint` (`/jobs/next`), `upload_endpoint`
(`/jobs/{job_id}/result`), and `fail_endpoint` (`/jobs/{job_id}/fail`)
already match this server's routes by default - no need to change those.
Each pipeline's own config sets the `type` it polls for.

Point more than one machine running the same pipeline at this deployment to
spread work across multiple GPUs - see `minimax-h3-windows`'s README
section on running on multiple devices for how job distribution and
failover behave (applies the same way per-type here).

## Note on Render's ephemeral disk

If deployed on Render (or similar) **without an attached persistent disk**,
`data/` - the job queue and every stored result - is wiped on every
redeploy. Fine for a demo; for anything you don't want to lose, attach a
persistent disk and mount it at this app's `data/` directory (the path is
hardcoded relative to `app.py`, not currently an env var).
