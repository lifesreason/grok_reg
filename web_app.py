from pathlib import Path
from threading import Lock

from fastapi import FastAPI, HTTPException, Query, Request
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
    "cpa_management_key",
    "yyds_api_key",
    "yyds_jwt",
    "email_webhook_secret",
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
    if not job:
        return False
    try:
        if job.status().get("status") not in {"pending", "running"}:
            return False
        return bool(job.thread and job.thread.is_alive())
    except Exception:
        return job.status().get("status") in {"pending", "running"}


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


@app.get("/api/mail-domain-pool")
def mail_domain_pool_status():
    """域名内存池运行时状态（对齐 openai-cpa 统计）。"""
    try:
        import mail_domain_pool as mdp

        settings = mdp.settings_from_config(reg.load_config())
        return {"ok": True, "summary": mdp.runtime_summary(settings)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/mail-domain-pool/reset")
def mail_domain_pool_reset():
    try:
        import mail_domain_pool as mdp

        mdp.reset_runtime()
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/mail-domain-pool/clear-domain")
def mail_domain_pool_clear_domain(payload: dict):
    domain = str((payload or {}).get("domain") or "").strip()
    if not domain:
        raise HTTPException(status_code=400, detail="domain required")
    try:
        import mail_domain_pool as mdp

        return {"ok": True, "result": mdp.clear_domain_counters(domain)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/webhook/email")
async def webhook_email(request: Request):
    """兼容 openai-cpa-email Worker 推送。

    Header: X-Webhook-Secret
    Body: {message_id, to_addr, raw_content}
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    headers = request.headers

    settings = reg.load_config()
    secret_cfg = str(settings.get("email_webhook_secret") or "").strip()
    if not secret_cfg:
        raise HTTPException(status_code=503, detail="email_webhook_secret 未配置")

    header_secret = str(
        headers.get("x-webhook-secret")
        or headers.get("X-Webhook-Secret")
        or ""
    ).strip()
    body_secret = str((payload or {}).get("secret") or "").strip()
    if header_secret != secret_cfg and body_secret != secret_cfg:
        raise HTTPException(status_code=401, detail="invalid webhook secret")

    to_addr = str((payload or {}).get("to_addr") or (payload or {}).get("to") or "").strip()
    raw_content = str((payload or {}).get("raw_content") or (payload or {}).get("raw") or "")
    message_id = str((payload or {}).get("message_id") or (payload or {}).get("id") or "").strip()
    if not to_addr or not raw_content:
        raise HTTPException(status_code=400, detail="to_addr and raw_content required")

    try:
        import webhook_mail_store as wms

        result = wms.store_webhook_mail(
            to_addr=to_addr,
            raw_content=raw_content,
            message_id=message_id,
        )
        return {"ok": True, **result, "stats": wms.stats()}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/webhook/email/stats")
def webhook_email_stats():
    try:
        import webhook_mail_store as wms

        return {"ok": True, "stats": wms.stats()}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


def public_account(account):
    item = dict(account)
    item.pop("sso", None)
    item.pop("refresh_token", None)
    return item


@app.get("/api/accounts")
def list_accounts():
    accounts = reg.list_registered_accounts(include_sso=False)
    return {"total": len(accounts), "accounts": accounts}


@app.delete("/api/accounts")
def delete_accounts(payload: dict):
    try:
        result = reg.delete_registered_accounts(payload.get("account_ids") or [])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"删除账号失败: {exc}")
    if not result["deleted"]:
        raise HTTPException(status_code=404, detail="未找到选中的账号")
    accounts = reg.list_registered_accounts(include_sso=False)
    return {
        **result,
        "status": "deleted",
        "message": f"已删除 {result['deleted']} 个账号",
        "accounts": [public_account(account) for account in accounts],
    }


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


@app.post("/api/accounts/import/grok2api")
def import_accounts_to_grok2api(payload: dict):
    settings = merge_sensitive_values(payload)
    account_ids = payload.get("account_ids") or []
    accounts = reg.find_registered_accounts(account_ids)
    if not accounts:
        raise HTTPException(status_code=404, detail="未找到选中的账号")
    try:
        result = reg.import_accounts_to_grok2api(accounts, settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"导入 grok2api 失败: {exc}")
    reg.persist_grok2api_push_status(accounts, result)
    accounts = reg.find_registered_accounts(account_ids)
    total = int(result.get("total") or len(accounts))
    failed = int(result.get("failed") or 0)
    status = "partial_failed" if failed else "pushed"
    message = f"已推送到 grok2api：{total} 个账号"
    if failed:
        message = f"grok2api 推送完成：成功 {total} 个，失败 {failed} 个"
    return {
        **result,
        "status": status,
        "message": message,
        "accounts": [public_account(account) for account in accounts],
    }


@app.post("/api/accounts/import/cpa")
def import_accounts_to_cpa(payload: dict):
    settings = merge_sensitive_values(payload)
    account_ids = payload.get("account_ids") or []
    accounts = reg.find_registered_accounts(account_ids)
    if not accounts:
        raise HTTPException(status_code=404, detail="未找到选中的账号")
    try:
        result = reg.import_accounts_to_cpa(accounts, settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"推送 CPA 失败: {exc}")
    reg.persist_cpa_push_status(accounts, result)
    accounts = reg.find_registered_accounts(account_ids)
    total = int(result.get("total") or 0)
    failed = int(result.get("failed") or 0)
    status = "partial_failed" if failed else "pushed"
    message = f"已推送到 CPA：{total} 个账号"
    if failed:
        message = f"CPA 推送完成：成功 {total} 个，失败 {failed} 个"
    return {
        **result,
        "status": status,
        "message": message,
        "accounts": [public_account(account) for account in accounts],
    }


@app.post("/api/accounts/check-health")
def check_accounts_health(payload: dict):
    settings = merge_sensitive_values(payload)
    account_ids = payload.get("account_ids") or []
    accounts = reg.find_registered_accounts(account_ids)
    if not accounts:
        raise HTTPException(status_code=404, detail="未找到选中的账号")
    try:
        result = reg.check_registered_accounts_health(accounts, settings)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"健康检查失败: {exc}")
    reg.persist_account_health_status(accounts, result)
    accounts = reg.find_registered_accounts(account_ids)
    healthy = int(result.get("healthy") or 0)
    failed = int(result.get("failed") or 0)
    status = "partial_failed" if failed else "healthy"
    return {
        **result,
        "status": status,
        "message": f"健康检查完成：可用 {healthy} 个，异常 {failed} 个",
        "accounts": [public_account(account) for account in accounts],
    }


def _resolve_job(job_id: str):
    """内存中的 job，或从落盘恢复的只读快照。"""
    job = _jobs.get(job_id)
    if job is not None:
        return job, False
    snapshot = reg.load_job_snapshot(job_id)
    if snapshot is None:
        return None, False
    return snapshot, True


@app.get("/api/jobs/current")
def get_current_job():
    """页面刷新后恢复：优先返回运行中任务；落盘 running 若进程已无则标为中断。"""
    with _job_lock:
        job_id = _active_job_id
        job = _jobs.get(job_id) if job_id else None
        if job is not None:
            st = job.status()
            alive = False
            try:
                alive = bool(job.thread and job.thread.is_alive())
            except Exception:
                alive = st.get("status") in {"pending", "running"}
            running = st.get("status") in {"pending", "running"} and alive
            # 线程已死但状态还是 running：纠正
            if st.get("status") in {"pending", "running"} and not alive:
                try:
                    job.status_value = "interrupted"
                    job.finished_at = job.finished_at or __import__("datetime").datetime.now().isoformat(timespec="seconds")
                    job._persist_status()
                except Exception:
                    pass
                st = job.status()
                running = False
            return {
                "job_id": job.id,
                "has_job": True,
                "running": running,
                **st,
            }

        # 内存没有：读落盘 current（只读历史，不能当 running）
        meta = reg.load_current_job_meta()
        if not meta:
            return {"has_job": False, "job_id": None, "status": "idle", "running": False}
        job_id = str(meta.get("job_id") or "").strip()
        snapshot = reg.load_job_snapshot(job_id) if job_id else None
        if not snapshot:
            return {"has_job": False, "job_id": job_id or None, "status": "idle", "running": False}
        st = dict(snapshot) if isinstance(snapshot, dict) else {}
        disk_status = str(st.get("status") or meta.get("status") or "finished")
        # 进程重启后磁盘上的 running 是僵尸状态
        if disk_status in {"pending", "running"}:
            disk_status = "interrupted"
            try:
                st["status"] = "interrupted"
                reg.save_job_snapshot(job_id, st)
                reg.save_current_job_meta(
                    {
                        "job_id": job_id,
                        "status": "interrupted",
                        "success_count": st.get("success_count"),
                        "fail_count": st.get("fail_count"),
                    }
                )
            except Exception:
                pass
        return {
            "job_id": job_id,
            "has_job": True,
            "running": False,
            "from_disk": True,
            "status": disk_status,
            "success_count": int(st.get("success_count") or meta.get("success_count") or 0),
            "fail_count": int(st.get("fail_count") or meta.get("fail_count") or 0),
            "register_count": st.get("register_count"),
            "register_threads": st.get("register_threads"),
            "output_file": st.get("output_file"),
            "created_at": st.get("created_at"),
            "started_at": st.get("started_at"),
            "finished_at": st.get("finished_at"),
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
            job = _jobs.get(_active_job_id)
            if job is not None:
                return {
                    "job_id": job.id,
                    "already_running": True,
                    **job.status(),
                }
            raise HTTPException(status_code=409, detail="已有任务正在运行")
        reg.config = validated
        reg.save_config()
        job = reg.RegistrationJob(validated)
        _jobs[job.id] = job
        _active_job_id = job.id
        job.start()
        return {"job_id": job.id, **job.status()}


def _do_stop_job(job_id: str = None):
    """停止任务：优先指定 id，否则停当前 active；找不到则清理僵尸状态。"""
    global _active_job_id
    with _job_lock:
        target_id = str(job_id or "").strip() or str(_active_job_id or "").strip()
        job = _jobs.get(target_id) if target_id else None
        if job is None and _active_job_id and _active_job_id != target_id:
            job = _jobs.get(_active_job_id)
            if job is not None:
                target_id = _active_job_id
        if job is not None:
            job.stop()
            st = job.status()
            return {"ok": True, "job_id": target_id, **st}

        meta = reg.load_current_job_meta() or {}
        disk_id = str(meta.get("job_id") or target_id or "").strip()
        snapshot = reg.load_job_snapshot(disk_id) if disk_id else None
        if isinstance(snapshot, dict):
            snapshot["status"] = "stopped"
            snapshot["stop_requested"] = True
            try:
                reg.save_job_snapshot(disk_id, snapshot)
            except Exception:
                pass
        if disk_id:
            try:
                reg.save_current_job_meta(
                    {
                        "job_id": disk_id,
                        "status": "stopped",
                        "success_count": (snapshot or {}).get("success_count")
                        or meta.get("success_count"),
                        "fail_count": (snapshot or {}).get("fail_count")
                        or meta.get("fail_count"),
                    }
                )
            except Exception:
                pass
        _active_job_id = None
        return {
            "ok": True,
            "job_id": disk_id or target_id or None,
            "status": "stopped",
            "message": "无运行中任务，已清理状态",
            "from_disk": True,
            "success_count": int((snapshot or {}).get("success_count") or meta.get("success_count") or 0),
            "fail_count": int((snapshot or {}).get("fail_count") or meta.get("fail_count") or 0),
        }


@app.post("/api/jobs/stop")
def stop_current_job():
    return _do_stop_job(None)


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str):
    return _do_stop_job(job_id)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job, from_disk = _resolve_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if from_disk:
        st = dict(job) if isinstance(job, dict) else {}
        st.setdefault("id", job_id)
        st["from_disk"] = True
        # 磁盘上的 running 不可信
        if str(st.get("status") or "") in {"pending", "running"}:
            st["status"] = "interrupted"
            st["running"] = False
        return st
    st = job.status()
    try:
        alive = bool(job.thread and job.thread.is_alive())
    except Exception:
        alive = st.get("status") in {"pending", "running"}
    if st.get("status") in {"pending", "running"} and not alive:
        st = dict(st)
        st["status"] = "interrupted"
    return st


@app.get("/api/jobs/{job_id}/logs")
def get_job_logs(job_id: str, offset: int = Query(0, ge=0)):
    job, from_disk = _resolve_job(job_id)
    if job is None:
        # 仅日志文件
        lines = reg.read_job_log_lines(job_id, offset=offset)
        return {"offset": offset, "next_offset": offset + len(lines), "lines": lines, "from_disk": True}
    if from_disk:
        lines = reg.read_job_log_lines(job_id, offset=offset)
        return {"offset": offset, "next_offset": offset + len(lines), "lines": lines, "from_disk": True}
    lines = job.logs(offset=offset)
    return {"offset": offset, "next_offset": offset + len(lines), "lines": lines}
