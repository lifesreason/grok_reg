import time

import pytest

import grok_register_ttk as reg


def wait_for_job(job, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = job.status()
        if status["status"] in {"completed", "failed", "stopped"}:
            return status
        time.sleep(0.02)
    raise AssertionError(f"job did not finish in time: {job.status()}")


def test_validate_registration_config_requires_cloudflare_base():
    with pytest.raises(ValueError, match="Cloudflare API Base"):
        reg.validate_registration_config(
            {"email_provider": "cloudflare", "register_count": 1, "register_threads": 1}
        )


def test_validate_registration_config_normalizes_counts_and_paths():
    settings = reg.validate_registration_config(
        {
            "email_provider": "duckmail",
            "register_count": "3",
            "register_threads": "20",
            "cloudflare_paths": "api/domains,api/new_address,api/token,api/mails",
        }
    )

    assert settings["register_count"] == 3
    assert settings["register_threads"] == 10
    assert settings["cloudflare_path_domains"] == "/api/domains"
    assert settings["cloudflare_path_accounts"] == "/api/new_address"
    assert settings["cloudflare_path_token"] == "/api/token"
    assert settings["cloudflare_path_messages"] == "/api/mails"


def test_registration_job_runs_successfully(monkeypatch, tmp_path):
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

    job = reg.RegistrationJob({"email_provider": "duckmail", "register_count": 1, "register_threads": 1})
    job.start()
    status = wait_for_job(job)

    assert status["status"] == "completed"
    assert status["success_count"] == 1
    assert status["fail_count"] == 0
    assert status["output_file"].endswith(".txt")
    assert "user@example.com----secret----sso-token" in tmp_path.joinpath(status["output_file"]).read_text(
        encoding="utf-8"
    )
    assert any("注册成功" in line for line in job.logs())


def test_registration_job_stop_request_sets_stopped_status(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(reg, "start_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "stop_browser", lambda: None)

    def slow_signup(log_callback=None, cancel_callback=None):
        while not cancel_callback():
            time.sleep(0.01)
        raise reg.RegistrationCancelled("stopped")

    monkeypatch.setattr(reg, "open_signup_page", slow_signup)

    job = reg.RegistrationJob({"email_provider": "duckmail", "register_count": 1, "register_threads": 1})
    job.start()
    time.sleep(0.05)
    job.stop()
    status = wait_for_job(job)

    assert status["status"] == "stopped"
    assert status["stop_requested"] is True
