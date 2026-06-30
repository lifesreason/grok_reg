from pathlib import Path
from threading import Lock

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import grok_register_ttk as reg


ROOT = Path(__file__).resolve().parent
SENSITIVE_KEYS = {
    "duckmail_api_key",
    "cloudflare_api_key",
    "cloudmail_password",
    "grok2api_remote_app_key",
    "sub2api_admin_token",
    "yyds_api_key",
    "yyds_jwt",
}

app = FastAPI(title="Grok Register Web", version="1.0.0")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

_jobs = {}
_active_job_id = None
_job_lock = Lock()


def mask_config(settings):
    masked = dict(settings)
    for key in SENSITIVE_KEYS:
        value = str(masked.get(key) or "")
        if value:
            masked[key] = "********"
    return masked


def merge_sensitive_values(new_settings):
    current = reg.load_config()
    merged = {**current, **dict(new_settings or {})}
    for key in SENSITIVE_KEYS:
        if merged.get(key) == "********":
            merged[key] = current.get(key, "")
    return merged


def active_job_running():
    if not _active_job_id:
        return False
    job = _jobs.get(_active_job_id)
    return bool(job and job.status()["status"] in {"pending", "running"})


@app.get("/")
def index():
    return FileResponse(ROOT / "templates" / "index.html")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/config")
def get_config():
    return mask_config(reg.load_config())


@app.put("/api/config")
def update_config(payload: dict):
    settings = merge_sensitive_values(payload)
    try:
        validated = reg.validate_registration_config(settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    reg.config = validated
    reg.save_config()
    return mask_config(validated)


def public_account(account):
    item = dict(account)
    item.pop("sso", None)
    return item


@app.get("/api/accounts")
def list_accounts():
    accounts = reg.list_registered_accounts(include_sso=False)
    return {"total": len(accounts), "accounts": accounts}


@app.post("/api/accounts/import/sub2api")
def import_accounts_to_sub2api(payload: dict):
    settings = merge_sensitive_values(payload)
    account_ids = payload.get("account_ids") or []
    accounts = reg.find_registered_accounts(account_ids)
    if not accounts:
        raise HTTPException(status_code=404, detail="未找到选中的账号")
    try:
        result = reg.import_accounts_to_sub2api(accounts, settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"导入 sub2api 失败: {exc}")
    reg.persist_sub2api_push_status(accounts, result)
    accounts = reg.find_registered_accounts(account_ids)
    total = int(result.get("total") or len(accounts))
    failed = int(result.get("failed") or 0)
    status = "partial_failed" if failed else "pushed"
    message = f"已推送到 sub2api：{total} 个账号"
    if failed:
        message = f"sub2api 推送完成：成功 {total} 个，失败 {failed} 个"
    return {
        **result,
        "status": status,
        "message": message,
        "accounts": [public_account(account) for account in accounts],
    }


@app.post("/api/jobs/start")
def start_job(payload: dict):
    global _active_job_id
    settings = merge_sensitive_values(payload)
    try:
        validated = reg.validate_registration_config(settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    with _job_lock:
        if active_job_running():
            raise HTTPException(status_code=409, detail="已有任务正在运行")
        reg.config = validated
        reg.save_config()
        job = reg.RegistrationJob(validated)
        _jobs[job.id] = job
        _active_job_id = job.id
        job.start()
        return {"job_id": job.id, **job.status()}


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    job.stop()
    return job.status()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job.status()


@app.get("/api/jobs/{job_id}/logs")
def get_job_logs(job_id: str, offset: int = Query(0, ge=0)):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    lines = job.logs(offset=offset)
    return {"offset": offset, "next_offset": offset + len(lines), "lines": lines}
