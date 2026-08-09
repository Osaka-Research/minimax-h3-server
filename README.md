# video-gen server

The "remote UI" the Windows pipeline (`../pipeline.py`) talks to: a page to
submit prompts, a job queue, and where the finished videos land.

- `GET /` — web page: submit a prompt, see queued/in-progress/done jobs, play
  finished videos inline.
- `GET /jobs/next` — polled by the Windows pipeline; returns the oldest
  queued job and marks it claimed, or 204 if none.
- `POST /jobs/<job_id>/result` — the Windows pipeline uploads the finished
  video here.
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
- Pipeline routes (`/jobs/next`, `/jobs/<id>/result`): `X-API-Key` header.

## Run locally

```bash
cd server
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

## Deploying so the Windows machine can reach it

This needs to be reachable from wherever the Windows pipeline runs -
options include a small VPS, a platform like Render/Railway/Fly.io, or a
tunnel (ngrok/Cloudflare Tunnel) to a machine on your own network. Any of
these work the same way: set `API_KEY`, run the app (ideally via gunicorn),
and get a public HTTPS URL.

Once you have that URL, on the Windows side edit `config.json`:

```jsonc
{
  "remote_ui_base_url": "https://your-deployed-url.example",
  "remote_api_key": "the-same-API_KEY-value"
}
```

`fetch_prompt_endpoint` (`/jobs/next`) and `upload_endpoint`
(`/jobs/{job_id}/result`) already match this server's routes by default -
no need to change those.
