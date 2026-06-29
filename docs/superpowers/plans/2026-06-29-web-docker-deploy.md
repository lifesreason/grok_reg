# Grok Register Web Docker Deploy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a FastAPI Web control panel, Docker Compose deployment files, and GitHub Actions image publishing for the existing Grok registration automation.

**Architecture:** Keep the existing browser and email automation helpers, add a reusable non-GUI job runner, then expose it through FastAPI. Store config and generated output in a data directory so Docker Compose can persist state.

**Tech Stack:** Python 3.12, FastAPI, Uvicorn, DrissionPage, curl_cffi, pytest, Docker, GitHub Actions, GHCR.

---

## File Structure

- `grok_register_ttk.py`: add `RegistrationJob`, `validate_registration_config`, data-dir aware config/output helpers, and route the existing Tkinter UI through the shared job runner.
- `web_app.py`: create FastAPI routes and single active-job manager.
- `templates/index.html`: create the Web dashboard markup.
- `static/app.css`: create responsive operator-console styling.
- `static/app.js`: implement config save, job start/stop, status polling, and log polling.
- `tests/test_registration_job.py`: test validation and job lifecycle with monkeypatched automation.
- `tests/test_web_app.py`: test API endpoints with `TestClient`.
- `requirements.txt`: list runtime/test dependencies.
- `Dockerfile`: build a Chromium-capable Python runtime.
- `docker-compose.yml`: provide NAS deployment example.
- `.github/workflows/docker-image.yml`: build and push GHCR image on `master`.
- `README.md`: update setup, local run, Docker Compose, and GHCR deployment instructions.

## Tasks

### Task 1: Add Tests for Shared Job and API

- [ ] Create `tests/test_registration_job.py` with tests for provider validation, successful one-account job execution using monkeypatched automation functions, and stop request behavior.
- [ ] Create `tests/test_web_app.py` with tests for `GET /healthz`, `GET /api/config`, `PUT /api/config`, `POST /api/jobs/start`, duplicate active-job rejection, `GET /api/jobs/{id}`, and logs offset behavior.
- [ ] Run `pytest -q` and confirm tests fail because `RegistrationJob` and `web_app` do not exist yet.

### Task 2: Implement Shared Registration Job

- [ ] Add data-directory helpers controlled by `GROK_REG_DATA_DIR`.
- [ ] Add `validate_registration_config(settings)` that enforces Cloudflare and CloudMail required fields and normalizes thread/count limits.
- [ ] Add `RegistrationJob` with background worker orchestration, logs list, counters, output files, cancellation, and status serialization.
- [ ] Update the Tkinter `GrokRegisterGUI` to start `RegistrationJob` instead of owning duplicate worker logic.
- [ ] Run `pytest tests/test_registration_job.py -q` and confirm it passes.

### Task 3: Implement FastAPI Web App and UI

- [ ] Create `web_app.py` with config and job endpoints.
- [ ] Create `templates/index.html`, `static/app.css`, and `static/app.js`.
- [ ] Run `pytest tests/test_web_app.py -q` and confirm it passes.
- [ ] Run the app locally and hit `GET /healthz`.

### Task 4: Add Docker, Compose, Actions, and Docs

- [ ] Create `requirements.txt`.
- [ ] Create `Dockerfile`.
- [ ] Create `docker-compose.yml`.
- [ ] Create `.github/workflows/docker-image.yml`.
- [ ] Update `README.md` with local, Docker Compose, and GHCR deployment instructions.
- [ ] Run `python -m compileall grok_register_ttk.py web_app.py`.
- [ ] Run `pytest -q`.
- [ ] Run `docker build -t grok-reg:test .` if Docker is available.

### Task 5: GitHub Repository, Commit, and Push

- [ ] Run `git status --short` and inspect staged changes before committing.
- [ ] Stage only intended files.
- [ ] Commit with a clear message.
- [ ] Create a new public GitHub repository using `gh repo create`.
- [ ] Push `master` to the new repository.
- [ ] Confirm remote URL and pushed commit hash.
