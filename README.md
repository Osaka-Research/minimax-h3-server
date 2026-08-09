# minimax-h3-server

The "remote UI" companion to
[`minimax-h3-windows`](https://github.com/Osaka-Research/minimax-h3-windows):
a page to submit video-generation prompts, a job queue, and where the
finished videos land. Deploy this anywhere reachable over HTTPS (a VPS, a
hosting platform, a tunnel); one or more Windows machines running
`minimax-h3-windows` poll it for work and upload results back.

- `GET /` — web page: submit a prompt, see queued/in-progress/done jobs, play
  finished videos inline. Shows which device claimed each job.
- `GET /jobs/next` — polled by the Windows pipeline; returns the oldest
  queued job and marks it claimed, or 204 if none. Race-safe under multiple
  concurrent devices polling at once (verified under `gunicorn -w 4` with
  20 simultaneous requests against 5 jobs — each claimed exactly once).
- `POST /jobs/<job_id>/result` — the Windows pipeline uploads the finished
  video here.
- `POST /jobs/<job_id>/fail` — the Windows pipeline reports a failed job;
  re-queued automatically up to `MAX_JOB_RETRIES` before being marked
  permanently failed.
- `GET /jobs/<job_id>` — JSON status for one job.
- `GET /videos/<job_id>.mp4` — the stored result.

Jobs are stored in a local SQLite file (`data/jobs.db`); videos in
`data/videos/`. Both are gitignored - this is local state, not something to
commit.

## Auth

Everything requires the shared secret in the `API_KEY` env var — without
this, anyone who found the URL could queue unlimited generation jobs on
your GPU or watch other people's videos. Two ways to send it, checked
against the same value:
- Browser routes (`/`, submitting the form, `/jobs/<id>`, `/videos/...`):
  HTTP Basic Auth — any username, password = `API_KEY`. Browsers will just
  prompt for it.
- Pipeline routes (`/jobs/next`, `/jobs/<id>/result`, `/jobs/<id>/fail`):
  `X-API-Key` header.

## Run locally

```bash
pip install -r requirements.txt
API_KEY=$(openssl rand -hex 20) python app.py   # prints nothing - pick your own and remember it
```

Or set a specific key: `API_KEY=your-secret-here python app.py`. Listens on
`0.0.0.0:8000` (override with `PORT`).

For anything beyond local testing, run it behind a real WSGI server instead
of Flask's dev server:

```bash
API_KEY=your-secret-here gunicorn -w 2 -b 0.0.0.0:8000 app:app
```

Optional tuning env vars: `MAX_JOB_RETRIES` (default 3),
`STALE_CLAIM_TIMEOUT_MINUTES` (default 30, for reclaiming jobs whose worker
crashed without reporting failure).

## Deploying so the Windows machine(s) can reach it

This needs to be reachable from wherever `minimax-h3-windows` runs -
options include a small VPS, a platform like Render/Railway/Fly.io, or a
tunnel (ngrok/Cloudflare Tunnel) to a machine on your own network. Any of
these work the same way: set `API_KEY`, run the app (ideally via gunicorn),
and get a public HTTPS URL.

Once you have that URL, on each Windows machine edit `config.json`:

```jsonc
{
  "remote_ui_base_url": "https://your-deployed-url.example",
  "remote_api_key": "the-same-API_KEY-value"
}
```

`fetch_prompt_endpoint` (`/jobs/next`), `upload_endpoint`
(`/jobs/{job_id}/result`), and `fail_endpoint` (`/jobs/{job_id}/fail`)
already match this server's routes by default - no need to change those.

Point more than one Windows machine at the same deployment to spread work
across multiple GPUs - see `minimax-h3-windows`'s README section on running
on multiple devices for how job distribution and failover behave.
