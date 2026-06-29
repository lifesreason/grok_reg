# Grok Register Web Docker Deploy Design

## Goal

Convert the existing Tkinter-only registration tool into a browser-operated Web service that can run on a NAS with Docker Compose. GitHub Actions will build and publish the Docker image to GitHub Container Registry on pushes to `master`.

## Architecture

The existing registration helper functions remain in `grok_register_ttk.py`. A new non-GUI `RegistrationJob` class owns job state, background worker threads, logs, counters, cancellation, and output files. The old Tkinter UI can continue to use the same automation path, while a new FastAPI app exposes a Web dashboard and JSON endpoints.

The Web service is intentionally simple: one process serves HTML, static assets, API endpoints, and background registration jobs. State is process-local. Persistent configuration and generated account files are stored under a configurable data directory, defaulting to the repository directory locally and `/app/data` in Docker.

## Components

- `grok_register_ttk.py`: keep browser/email/Grok automation helpers and add reusable job orchestration that has no Tkinter dependency.
- `web_app.py`: FastAPI application with config, job start/stop/status/log endpoints and the dashboard route.
- `templates/index.html`: Web control panel for editing config, starting/stopping work, and reading logs.
- `static/app.css` and `static/app.js`: operator-focused UI styling and browser-side polling.
- `requirements.txt`: Python dependencies for automation, FastAPI, tests, and runtime server.
- `Dockerfile`: Debian-based Python image with Chromium/runtime libraries and the app entrypoint.
- `docker-compose.yml`: NAS deployment example with persistent `./data` volume and port `8787`.
- `.github/workflows/docker-image.yml`: GHCR image build and push workflow.

## API Design

- `GET /`: returns the Web dashboard.
- `GET /healthz`: returns service health.
- `GET /api/config`: returns the current config with sensitive values masked.
- `PUT /api/config`: validates and saves config.
- `POST /api/jobs/start`: saves supplied config, validates provider settings, starts a background job, and returns its id.
- `POST /api/jobs/{job_id}/stop`: requests cancellation for the active job.
- `GET /api/jobs/{job_id}`: returns status, counters, output path, and timestamps.
- `GET /api/jobs/{job_id}/logs?offset=N`: returns log lines from the requested offset.

Only one registration job runs at a time in the first version. This matches the existing desktop UX and avoids multiple browser pools fighting over Chromium resources.

## Docker Design

The image installs Python dependencies and Chromium libraries, copies the project, and runs:

```bash
uvicorn web_app:app --host 0.0.0.0 --port 8787
```

The compose file mounts `./data:/app/data`, sets `GROK_REG_DATA_DIR=/app/data`, exposes `8787:8787`, and provides shared memory size for browser stability.

## Testing

Add API/job tests using FastAPI `TestClient` and monkeypatches for the slow browser automation. Verification includes:

- Python compilation.
- Unit/API tests.
- Docker image build.
- GitHub Actions workflow syntax sanity where possible.

## Deployment Flow

1. Create a new public GitHub repository.
2. Push the current `master` branch.
3. GitHub Actions builds and pushes `ghcr.io/<owner>/<repo>:master` and `:latest`.
4. NAS uses `docker-compose.yml`, with the image field adjusted to the final GHCR image name if needed.
