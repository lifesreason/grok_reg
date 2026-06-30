import time

from fastapi.testclient import TestClient

import grok_register_ttk as reg


def wait_for_api_job(client, job_id, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        response.raise_for_status()
        payload = response.json()
        if payload["status"] in {"completed", "failed", "stopped"}:
            return payload
        time.sleep(0.02)
    raise AssertionError("API job did not finish")


def test_healthz():
    from web_app import app

    client = TestClient(app)
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_config_round_trip_masks_sensitive_values(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    from web_app import app

    client = TestClient(app)
    response = client.put(
        "/api/config",
        json={
            "email_provider": "cloudmail",
            "cloudmail_url": "https://mail.example.test",
            "cloudmail_admin_email": "admin@example.test",
            "cloudmail_password": "top-secret",
            "defaultDomains": "example.test",
            "register_count": 2,
            "register_threads": 1,
        },
    )

    assert response.status_code == 200
    assert response.json()["cloudmail_password"] == "********"
    response = client.get("/api/config")
    assert response.status_code == 200
    assert response.json()["cloudmail_password"] == "********"


def test_yyds_config_round_trip_masks_sensitive_values(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    from web_app import app

    client = TestClient(app)
    response = client.put(
        "/api/config",
        json={
            "email_provider": "yyds",
            "yyds_api_key": "api-key-value",
            "yyds_jwt": "jwt-value",
            "register_count": 1,
            "register_threads": 1,
        },
    )

    assert response.status_code == 200
    assert response.json()["yyds_api_key"] == "********"
    assert response.json()["yyds_jwt"] == "********"

    saved = tmp_path.joinpath("config.json").read_text(encoding="utf-8")
    assert "api-key-value" in saved
    assert "jwt-value" in saved


def test_start_job_rejects_duplicate_active_job(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(reg, "start_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "stop_browser", lambda: None)

    def slow_signup(log_callback=None, cancel_callback=None):
        while not cancel_callback():
            time.sleep(0.01)
        raise reg.RegistrationCancelled("stopped")

    monkeypatch.setattr(reg, "open_signup_page", slow_signup)
    from web_app import app

    client = TestClient(app)
    response = client.post(
        "/api/jobs/start",
        json={"email_provider": "duckmail", "register_count": 1, "register_threads": 1},
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    duplicate = client.post(
        "/api/jobs/start",
        json={"email_provider": "duckmail", "register_count": 1, "register_threads": 1},
    )
    assert duplicate.status_code == 409

    stop_response = client.post(f"/api/jobs/{job_id}/stop")
    assert stop_response.status_code == 200
    assert wait_for_api_job(client, job_id)["status"] == "stopped"


def test_job_status_and_logs(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(reg, "start_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "restart_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "stop_browser", lambda: None)
    monkeypatch.setattr(reg, "open_signup_page", lambda log_callback=None, cancel_callback=None: None)
    monkeypatch.setattr(
        reg,
        "fill_email_and_submit",
        lambda log_callback=None, cancel_callback=None: ("user@example.com", "mail-token"),
    )
    monkeypatch.setattr(
        reg,
        "fill_code_and_submit",
        lambda email, token, log_callback=None, cancel_callback=None: "123456",
    )
    monkeypatch.setattr(
        reg,
        "fill_profile_and_submit",
        lambda log_callback=None, cancel_callback=None: {
            "given_name": "Ada",
            "family_name": "Lovelace",
            "password": "secret",
        },
    )
    monkeypatch.setattr(
        reg,
        "wait_for_sso_cookie",
        lambda log_callback=None, cancel_callback=None: "sso-token",
    )
    monkeypatch.setattr(
        reg,
        "add_token_to_grok2api_pools",
        lambda raw_token, email="", log_callback=None: None,
    )
    from web_app import app

    client = TestClient(app)
    response = client.post(
        "/api/jobs/start",
        json={"email_provider": "duckmail", "register_count": 1, "register_threads": 1},
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    status = wait_for_api_job(client, job_id)
    assert status["status"] == "completed"
    assert status["success_count"] == 1

    logs = client.get(f"/api/jobs/{job_id}/logs", params={"offset": 0})
    assert logs.status_code == 200
    payload = logs.json()
    assert payload["next_offset"] >= 1
    assert any("注册成功" in line for line in payload["lines"])

    tail = client.get(f"/api/jobs/{job_id}/logs", params={"offset": payload["next_offset"]})
    assert tail.status_code == 200
    assert tail.json()["lines"] == []
