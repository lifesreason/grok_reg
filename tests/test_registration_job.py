import base64
import json
import threading
import time
from pathlib import Path

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
    assert settings["grok2api_auto_add_remote"] is False
    assert settings["sub2api_auto_import_remote"] is False


def test_validate_registration_config_keeps_auto_push_switches():
    settings = reg.validate_registration_config(
        {
            "email_provider": "duckmail",
            "grok2api_auto_add_remote": True,
            "sub2api_auto_import_remote": True,
        }
    )

    assert settings["grok2api_auto_add_remote"] is True
    assert settings["sub2api_auto_import_remote"] is True


def test_validate_registration_config_limits_cpa_push_workers():
    settings = reg.validate_registration_config(
        {"email_provider": "duckmail", "cpa_push_workers": "99"}
    )

    assert settings["cpa_push_workers"] == 10


def test_validate_registration_config_normalizes_risk_controls():
    settings = reg.validate_registration_config(
        {
            "email_provider": "duckmail",
            "thread_start_interval": "99",
            "account_interval_seconds": "-3",
            "account_interval_jitter_seconds": "999",
            "stop_on_consecutive_blocks": "80",
            "enable_nsfw": "false",
        }
    )

    assert settings["thread_start_interval"] == 60.0
    assert settings["account_interval_seconds"] == 0.0
    assert settings["account_interval_jitter_seconds"] == 300.0
    assert settings["stop_on_consecutive_blocks"] == 50
    assert settings["enable_nsfw"] is False


def test_is_account_blocked_error_detects_xai_blocked_payload():
    assert reg.is_account_blocked_error(
        'xAI OAuth refresh HTTP 400: {"error":"invalid_grant","error_description":"User account is blocked"}'
    )
    assert reg.is_account_blocked_error("失败：账号已封禁")
    assert not reg.is_account_blocked_error(
        '{"error":"invalid_grant","error_description":"Refresh token has been revoked"}'
    )


def test_load_config_resets_defaults_when_data_directory_has_no_config(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        reg,
        "config",
        {**reg.DEFAULT_CONFIG, "cpa_auto_push_remote": True, "cpa_management_key": "stale-key"},
    )

    settings = reg.load_config()

    assert settings["cpa_auto_push_remote"] is False
    assert settings["cpa_management_key"] == ""


def test_delete_registered_accounts_removes_selected_account_and_migrates_remaining_statuses(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    source = tmp_path.joinpath("accounts_20260713_120000_job.txt")
    source.write_text(
        "first@example.com----Pass----sso-token-1----refresh-token-1\n"
        "remove@example.com----Pass----sso-token-2----refresh-token-2\n"
        "last@example.com----Pass----sso-token-3----refresh-token-3\n",
        encoding="utf-8",
    )
    first, removed, last = reg.list_registered_accounts()
    reg.save_account_statuses(
        {
            removed["id"]: {"cpa_status": "pushed", "email": removed["email"]},
            last["id"]: {"health_status": "healthy", "email": last["email"]},
        }
    )

    result = reg.delete_registered_accounts([removed["id"]])

    assert result["deleted"] == 1
    assert result["missing"] == 0
    assert source.read_text(encoding="utf-8") == (
        "first@example.com----Pass----sso-token-1----refresh-token-1\n"
        "last@example.com----Pass----sso-token-3----refresh-token-3\n"
    )
    current = reg.list_registered_accounts()
    assert [account["email"] for account in current] == ["first@example.com", "last@example.com"]
    assert current[0]["id"] == first["id"]
    assert current[1]["id"] != last["id"]
    statuses = reg.load_account_statuses()
    assert removed["id"] not in statuses
    assert last["id"] not in statuses
    assert statuses[current[1]["id"]]["health_status"] == "healthy"
    assert statuses[current[1]["id"]]["line_no"] == 2


def test_registration_job_runs_successfully(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(reg, "start_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "restart_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "stop_browser", lambda: None)
    monkeypatch.setattr(reg, "sleep_with_cancel", lambda seconds, cancel_callback=None: None)
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
        "fetch_xai_oauth_refresh_token",
        lambda sso, log_callback=None, cancel_callback=None: "refresh-token",
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
    assert "user@example.com----secret----sso-token----refresh-token" in tmp_path.joinpath(status["output_file"]).read_text(
        encoding="utf-8"
    )
    assert any("注册成功" in line for line in job.logs())


def test_registration_job_pushes_cpa_after_refresh_token_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(reg, "open_signup_page", lambda **kwargs: None)
    monkeypatch.setattr(reg, "fill_email_and_submit", lambda **kwargs: ("user@example.com", "mail-token"))
    monkeypatch.setattr(reg, "fill_code_and_submit", lambda *args, **kwargs: "123456")
    monkeypatch.setattr(
        reg,
        "fill_profile_and_submit",
        lambda **kwargs: {"given_name": "Ada", "family_name": "Lovelace", "password": "secret"},
    )
    monkeypatch.setattr(reg, "wait_for_sso_cookie", lambda **kwargs: "sso-token")
    monkeypatch.setattr(reg, "fetch_xai_oauth_refresh_token", lambda *args, **kwargs: "refresh-token")
    monkeypatch.setattr(reg, "add_token_to_grok2api_pools", lambda *args, **kwargs: None)
    monkeypatch.setattr(reg, "auto_push_registered_account", lambda *args, **kwargs: None)
    pushed = []
    monkeypatch.setattr(
        reg,
        "export_and_push_cpa_credential",
        lambda email, refresh_token, settings, log_callback=None: pushed.append(
            (email, refresh_token, settings)
        )
        or {"ok": True, "uploaded": True},
    )

    job = reg.RegistrationJob(
        {
            "email_provider": "duckmail",
            "register_count": 1,
            "register_threads": 1,
            "cpa_auto_push_remote": True,
            "cpa_management_base": "https://cpa.example.test",
            "cpa_management_key": "management-secret",
        }
    )
    job._run_single_registration(1, 1, lambda message: None)

    assert pushed == [("user@example.com", "refresh-token", job.settings)]


def test_registration_job_retries_when_profile_session_returns_to_signup(monkeypatch, tmp_path):
    emails = iter(["first@example.com", "second@example.com"])
    profile_calls = [0]

    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(reg, "start_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "restart_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "stop_browser", lambda: None)
    monkeypatch.setattr(reg, "sleep_with_cancel", lambda seconds, cancel_callback=None: None)
    monkeypatch.setattr(reg, "open_signup_page", lambda log_callback=None, cancel_callback=None: None)
    monkeypatch.setattr(
        reg,
        "fill_email_and_submit",
        lambda log_callback=None, cancel_callback=None: (next(emails), "mail-token"),
    )
    monkeypatch.setattr(
        reg,
        "fill_code_and_submit",
        lambda email, token, log_callback=None, cancel_callback=None: "123456",
    )

    def fake_fill_profile(log_callback=None, cancel_callback=None):
        profile_calls[0] += 1
        if profile_calls[0] == 1:
            raise reg.ProfileSessionLost("最终注册页退回注册入口")
        return {"given_name": "Ada", "family_name": "Lovelace", "password": "secret"}

    monkeypatch.setattr(reg, "fill_profile_and_submit", fake_fill_profile)
    monkeypatch.setattr(reg, "wait_for_sso_cookie", lambda log_callback=None, cancel_callback=None: "sso-token")
    monkeypatch.setattr(reg, "fetch_xai_oauth_refresh_token", lambda sso, log_callback=None, cancel_callback=None: "refresh-token")
    monkeypatch.setattr(reg, "add_token_to_grok2api_pools", lambda raw_token, email="", log_callback=None: None)

    job = reg.RegistrationJob({"email_provider": "duckmail", "register_count": 1, "register_threads": 1})
    job.start()
    status = wait_for_job(job)

    assert status["status"] == "completed"
    assert status["success_count"] == 1
    assert status["fail_count"] == 0
    assert any("注册会话丢失，自动换邮箱重试" in line for line in job.logs())
    assert "second@example.com----secret----sso-token----refresh-token" in tmp_path.joinpath(
        status["output_file"]
    ).read_text(encoding="utf-8")


def test_registration_job_enables_nsfw_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(reg, "start_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "restart_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "stop_browser", lambda: None)
    monkeypatch.setattr(reg, "sleep_with_cancel", lambda seconds, cancel_callback=None: None)
    monkeypatch.setattr(reg, "open_signup_page", lambda log_callback=None, cancel_callback=None: None)
    monkeypatch.setattr(reg, "fill_email_and_submit", lambda log_callback=None, cancel_callback=None: ("user@example.com", "mail-token"))
    monkeypatch.setattr(reg, "fill_code_and_submit", lambda email, token, log_callback=None, cancel_callback=None: "123456")
    monkeypatch.setattr(reg, "fill_profile_and_submit", lambda log_callback=None, cancel_callback=None: {"given_name": "Ada", "family_name": "Lovelace", "password": "secret"})
    monkeypatch.setattr(reg, "wait_for_sso_cookie", lambda log_callback=None, cancel_callback=None: "sso-token")
    monkeypatch.setattr(reg, "fetch_xai_oauth_refresh_token", lambda sso, log_callback=None, cancel_callback=None: "refresh-token")
    monkeypatch.setattr(reg, "add_token_to_grok2api_pools", lambda raw_token, email="", log_callback=None: None)
    calls = []

    def fake_enable(token, cf_clearance="", log_callback=None):
        calls.append((token, cf_clearance))
        return True, "ok"

    monkeypatch.setattr(reg, "enable_nsfw_for_token", fake_enable)

    job = reg.RegistrationJob(
        {"email_provider": "duckmail", "register_count": 1, "register_threads": 1, "enable_nsfw": True}
    )
    job.start()
    status = wait_for_job(job)

    assert status["status"] == "completed"
    assert calls == [("sso-token", "")]
    assert any("NSFW 已开启" in line for line in job.logs())


def test_registration_job_skips_nsfw_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(reg, "start_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "restart_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "stop_browser", lambda: None)
    monkeypatch.setattr(reg, "sleep_with_cancel", lambda seconds, cancel_callback=None: None)
    monkeypatch.setattr(reg, "open_signup_page", lambda log_callback=None, cancel_callback=None: None)
    monkeypatch.setattr(reg, "fill_email_and_submit", lambda log_callback=None, cancel_callback=None: ("user@example.com", "mail-token"))
    monkeypatch.setattr(reg, "fill_code_and_submit", lambda email, token, log_callback=None, cancel_callback=None: "123456")
    monkeypatch.setattr(reg, "fill_profile_and_submit", lambda log_callback=None, cancel_callback=None: {"given_name": "Ada", "family_name": "Lovelace", "password": "secret"})
    monkeypatch.setattr(reg, "wait_for_sso_cookie", lambda log_callback=None, cancel_callback=None: "sso-token")
    monkeypatch.setattr(reg, "fetch_xai_oauth_refresh_token", lambda sso, log_callback=None, cancel_callback=None: "refresh-token")
    monkeypatch.setattr(reg, "add_token_to_grok2api_pools", lambda raw_token, email="", log_callback=None: None)
    monkeypatch.setattr(reg, "enable_nsfw_for_token", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("NSFW should not run")))

    job = reg.RegistrationJob(
        {"email_provider": "duckmail", "register_count": 1, "register_threads": 1, "enable_nsfw": False}
    )
    job.start()
    status = wait_for_job(job)

    assert status["status"] == "completed"


def test_registration_job_auto_pushes_to_remote_services_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(reg, "start_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "restart_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "stop_browser", lambda: None)
    monkeypatch.setattr(reg, "sleep_with_cancel", lambda seconds, cancel_callback=None: None)
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
    monkeypatch.setattr(reg, "wait_for_sso_cookie", lambda log_callback=None, cancel_callback=None: "sso-token")
    monkeypatch.setattr(reg, "fetch_xai_oauth_refresh_token", lambda sso, log_callback=None, cancel_callback=None: "refresh-token")
    monkeypatch.setattr(reg, "add_token_to_grok2api_pools", lambda raw_token, email="", log_callback=None: None)
    pushed = []

    def fake_grok2api(accounts, settings=None, log_callback=None):
        pushed.append(("grok2api", accounts[0]["email"], settings["grok2api_remote_base"]))
        return {"items": [{"response": {"pool": "basic"}}], "total": 1, "failed": 0}

    def fake_sub2api(accounts, settings=None, log_callback=None):
        pushed.append(("sub2api", accounts[0]["email"], settings["sub2api_base"]))
        return {"items": [{"response": {"id": 101}}], "total": 1, "failed": 0}

    monkeypatch.setattr(reg, "import_accounts_to_grok2api", fake_grok2api)
    monkeypatch.setattr(reg, "import_accounts_to_sub2api", fake_sub2api)

    job = reg.RegistrationJob(
        {
            "email_provider": "duckmail",
            "register_count": 1,
            "register_threads": 1,
            "grok2api_auto_add_remote": True,
            "grok2api_remote_base": "http://grok2api.example",
            "grok2api_remote_app_key": "app-key",
            "sub2api_auto_import_remote": True,
            "sub2api_base": "http://sub2api.example",
            "sub2api_admin_token": "admin-key",
        }
    )
    job.start()
    status = wait_for_job(job)

    assert status["status"] == "completed"
    assert pushed == [
        ("grok2api", "user@example.com", "http://grok2api.example"),
        ("sub2api", "user@example.com", "http://sub2api.example"),
    ]
    account = reg.list_registered_accounts()[0]
    assert account["grok2api_status"] == "pushed"
    assert account["sub2api_status"] == "pushed"


def test_registration_job_does_not_auto_push_when_switches_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(reg, "start_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "restart_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "stop_browser", lambda: None)
    monkeypatch.setattr(reg, "sleep_with_cancel", lambda seconds, cancel_callback=None: None)
    monkeypatch.setattr(reg, "open_signup_page", lambda log_callback=None, cancel_callback=None: None)
    monkeypatch.setattr(reg, "fill_email_and_submit", lambda log_callback=None, cancel_callback=None: ("user@example.com", "mail-token"))
    monkeypatch.setattr(reg, "fill_code_and_submit", lambda email, token, log_callback=None, cancel_callback=None: "123456")
    monkeypatch.setattr(reg, "fill_profile_and_submit", lambda log_callback=None, cancel_callback=None: {"given_name": "Ada", "family_name": "Lovelace", "password": "secret"})
    monkeypatch.setattr(reg, "wait_for_sso_cookie", lambda log_callback=None, cancel_callback=None: "sso-token")
    monkeypatch.setattr(reg, "fetch_xai_oauth_refresh_token", lambda sso, log_callback=None, cancel_callback=None: "refresh-token")
    monkeypatch.setattr(reg, "add_token_to_grok2api_pools", lambda raw_token, email="", log_callback=None: None)
    monkeypatch.setattr(reg, "import_accounts_to_grok2api", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("grok2api should not run")))
    monkeypatch.setattr(reg, "import_accounts_to_sub2api", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("sub2api should not run")))

    job = reg.RegistrationJob({"email_provider": "duckmail", "register_count": 1, "register_threads": 1})
    job.start()
    status = wait_for_job(job)

    assert status["status"] == "completed"


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


def test_registration_job_stops_after_consecutive_account_blocks(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(reg, "start_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "stop_browser", lambda: None)
    monkeypatch.setattr(reg, "restart_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "sleep_with_cancel", lambda seconds, cancel_callback=None: None)

    def blocked_registration(self, idx, total, logf):
        raise Exception(
            'xAI OAuth refresh HTTP 400: {"error":"invalid_grant","error_description":"User account is blocked"}'
        )

    monkeypatch.setattr(reg.RegistrationJob, "_run_single_registration", blocked_registration)

    job = reg.RegistrationJob(
        {
            "email_provider": "duckmail",
            "register_count": 6,
            "register_threads": 1,
            "account_interval_seconds": 0,
            "account_interval_jitter_seconds": 0,
            "stop_on_consecutive_blocks": 3,
        }
    )
    job.start()
    status = wait_for_job(job)

    assert status["status"] == "failed"
    assert status["fail_count"] == 3
    assert status["block_stop_triggered"] is True
    assert status["consecutive_blocks"] == 3
    assert any("连续 3 个账号出现封禁信号" in line for line in job.logs())


def test_optional_tkinter_import_handles_missing_shared_library():
    source = Path(reg.__file__).read_text(encoding="utf-8")

    assert "except ImportError:" in source


def test_cloudflare_block_page_is_detected():
    html = """
    <html>
      <head><title>Attention Required! | Cloudflare</title></head>
      <body>Sorry, you have been blocked</body>
    </html>
    """

    assert reg.detect_cloudflare_block_page(html) is True


def test_email_form_script_supports_identifier_input_and_continue_button():
    script = reg.build_email_form_script("fill")

    assert 'input[name="identifier"]' in reg.EMAIL_INPUT_SELECTOR
    assert 'input[placeholder*="email" i]' in reg.EMAIL_INPUT_SELECTOR
    assert 'action = "fill"' in script

    submit_script = reg.build_email_form_script("submit")

    assert "continue" in reg.EMAIL_SUBMIT_KEYWORDS
    assert "next" in reg.EMAIL_SUBMIT_KEYWORDS
    assert 'action = "submit"' in submit_script


def test_wait_for_email_verification_step_waits_until_otp(monkeypatch):
    class FakePage:
        def __init__(self):
            self.states = ['{"step":"email","errorText":"","url":"https://accounts.x.ai/sign-up"}', '{"step":"otp","errorText":"","url":"https://accounts.x.ai/sign-up"}']

        def run_js(self, script, *args):
            return self.states.pop(0)

    sleeps = []
    monkeypatch.setattr(reg, "sleep_with_cancel", lambda seconds, cancel_callback=None: sleeps.append(seconds))

    assert reg.wait_for_email_verification_step(FakePage(), "user@example.com", timeout=5) == "otp"
    assert sleeps == [0.8]


def test_wait_for_email_verification_step_detects_rejected_domain(monkeypatch):
    class FakePage:
        def run_js(self, script, *args):
            return (
                '{"step":"email","errorText":"",'
                '"bodySnippet":"Your email domain duckmail.sbs has been rejected. Please use a different email address."}'
            )

    monkeypatch.setattr(reg, "sleep_with_cancel", lambda seconds, cancel_callback=None: None)

    with pytest.raises(reg.EmailDomainRejected) as exc:
        reg.wait_for_email_verification_step(FakePage(), "user@duckmail.sbs", timeout=0.1)

    assert exc.value.domain == "duckmail.sbs"


def test_wait_for_post_code_transition_returns_when_profile_form_appears(monkeypatch):
    states = iter(["waiting", "profile-form"])

    class FakePage:
        def run_js(self, script):
            assert "post-code-profile-form" in script
            return next(states)

    monkeypatch.setattr(reg, "sleep_with_cancel", lambda seconds, cancel_callback=None: None)

    assert reg.wait_for_post_code_transition(FakePage(), "user@example.com", timeout=5) == "profile-form"


def test_wait_for_post_code_transition_rejects_a_persistent_error_page(monkeypatch):
    now = [0.0]

    class FakePage:
        def run_js(self, script):
            return {
                "state": "post-code-error-page",
                "bodySnippet": "An error occurred There was an error loading this page.",
            }

    monkeypatch.setattr(reg.time, "time", lambda: now[0])
    monkeypatch.setattr(
        reg,
        "sleep_with_cancel",
        lambda seconds, cancel_callback=None: now.__setitem__(0, now[0] + seconds),
    )

    with pytest.raises(reg.ProfileSessionLost, match="持续停在错误页"):
        reg.wait_for_post_code_transition(FakePage(), "user@example.com", timeout=30)


def test_wait_for_post_code_transition_includes_error_resource_diagnostics(monkeypatch):
    now = [0.0]

    class FakePage:
        def run_js(self, script):
            return {
                "state": "post-code-error-page",
                "bodySnippet": "An error occurred There was an error loading this page.",
                "resourceSummary": {
                    "verifyEmailSeen": True,
                    "validatePasswordSeen": False,
                    "signupSeen": False,
                },
            }

    monkeypatch.setattr(reg.time, "time", lambda: now[0])
    monkeypatch.setattr(
        reg,
        "sleep_with_cancel",
        lambda seconds, cancel_callback=None: now.__setitem__(0, now[0] + seconds),
    )

    with pytest.raises(reg.ProfileSessionLost) as exc:
        reg.wait_for_post_code_transition(FakePage(), "user@example.com", timeout=30)

    assert "verifyEmailSeen" in str(exc.value)


def test_wait_for_post_code_transition_waits_through_error_page_after_verify(monkeypatch):
    states = iter(
        [
            {
                "state": "post-code-error-page",
                "bodySnippet": "An error occurred Retry",
                "resourceSummary": {"verifyEmailSeen": True},
                "retryTarget": {"centerX": 11, "centerY": 22, "text": "Retry"},
            },
            "profile-form",
        ]
    )

    class FakePage:
        def __init__(self):
            self.cdp_calls = []

        def run_js(self, script):
            return next(states)

        def run_cdp(self, method, **params):
            self.cdp_calls.append((method, params))

    page = FakePage()
    monkeypatch.setattr(reg, "sleep_with_cancel", lambda seconds, cancel_callback=None: None)

    assert reg.wait_for_post_code_transition(page, "user@example.com", timeout=5) == "profile-form"
    assert page.cdp_calls == []


def test_fill_code_waits_for_frontend_state_after_otp_input():
    source = Path("grok_register_ttk.py").read_text(encoding="utf-8")

    assert "验证码已填入，等待前端状态同步" in source
    assert "sleep_with_cancel(0.6, cancel_callback)" in source


def test_fill_code_falls_back_to_cdp_when_js_otp_fill_is_not_ready(monkeypatch):
    class FakePage:
        def __init__(self):
            self.cdp_calls = []

        def run_js(self, script, *args):
            if "otp-native-target" in script:
                return {
                    "state": "otp-target",
                    "mode": "aggregate",
                    "centerX": 101,
                    "centerY": 202,
                    "valueLen": 0,
                }
            if "otp-submit-target" in script:
                return {
                    "state": "otp-submit-target",
                    "centerX": 303,
                    "centerY": 404,
                    "text": "Continue",
                }
            if args:
                return "not-ready"
            return "clicked"

        def run_cdp(self, method, **params):
            self.cdp_calls.append((method, params))

    page = FakePage()
    transitions = []
    monkeypatch.setattr(reg, "_get_page", lambda: page)
    monkeypatch.setattr(reg, "get_oai_code", lambda *args, **kwargs: "TFF-KTN")
    monkeypatch.setattr(reg, "sleep_with_cancel", lambda seconds, cancel_callback=None: None)
    monkeypatch.setattr(
        reg,
        "wait_for_post_code_transition",
        lambda page_arg, email, log_callback=None, cancel_callback=None: transitions.append(email),
    )

    assert reg.fill_code_and_submit("user@example.com", "mail-token") == "TFF-KTN"

    typed = [
        params["text"]
        for method, params in page.cdp_calls
        if method == "Input.dispatchKeyEvent" and params.get("type") == "keyDown"
    ]
    assert typed == list("TFFKTN")
    assert any(
        method == "Input.dispatchMouseEvent"
        and params.get("type") == "mousePressed"
        and params.get("x") == 303
        and params.get("y") == 404
        for method, params in page.cdp_calls
    )
    assert transitions == ["user@example.com"]


def test_registration_job_retries_when_email_domain_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    reg._rejected_email_domains.clear()
    monkeypatch.setattr(reg, "start_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "restart_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "stop_browser", lambda: None)
    monkeypatch.setattr(reg, "sleep_with_cancel", lambda seconds, cancel_callback=None: None)
    monkeypatch.setattr(reg, "open_signup_page", lambda log_callback=None, cancel_callback=None: None)

    attempts = []

    def fake_fill_email(log_callback=None, cancel_callback=None):
        attempts.append(1)
        if len(attempts) == 1:
            raise reg.EmailDomainRejected("duckmail.sbs")
        return "user@example.com", "mail-token"

    monkeypatch.setattr(reg, "fill_email_and_submit", fake_fill_email)
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
    monkeypatch.setattr(reg, "wait_for_sso_cookie", lambda log_callback=None, cancel_callback=None: "sso-token")
    monkeypatch.setattr(
        reg,
        "fetch_xai_oauth_refresh_token",
        lambda sso, log_callback=None, cancel_callback=None: "refresh-token",
    )
    monkeypatch.setattr(reg, "add_token_to_grok2api_pools", lambda raw_token, email="", log_callback=None: None)

    job = reg.RegistrationJob({"email_provider": "duckmail", "register_count": 1, "register_threads": 1})
    job.start()
    status = wait_for_job(job)

    assert status["status"] == "completed"
    assert len(attempts) == 2
    assert any("邮箱域名被 x.ai 拒收" in line for line in job.logs())


def test_registration_job_stops_queue_when_email_provider_is_unusable(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(reg, "start_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "restart_browser", lambda log_callback=None: (object(), object()))
    monkeypatch.setattr(reg, "stop_browser", lambda: None)
    monkeypatch.setattr(reg, "sleep_with_cancel", lambda seconds, cancel_callback=None: None)
    monkeypatch.setattr(reg, "open_signup_page", lambda log_callback=None, cancel_callback=None: None)

    attempts = []

    def fake_fill_email(log_callback=None, cancel_callback=None):
        attempts.append(1)
        raise reg.EmailProviderUnavailable("DuckMail 可用域名已被 x.ai 拒收: duckmail.sbs")

    monkeypatch.setattr(reg, "fill_email_and_submit", fake_fill_email)

    job = reg.RegistrationJob({"email_provider": "duckmail", "register_count": 6, "register_threads": 1})
    job.start()
    status = wait_for_job(job)

    assert status["status"] == "failed"
    assert status["fail_count"] == 1
    assert len(attempts) == 1
    assert job.stop_requested is True
    assert any("邮箱服务商不可用" in line for line in job.logs())


def test_yyds_pick_domain_skips_rejected_domains_and_rotates(monkeypatch):
    reg._rejected_email_domains.clear()
    reg.remember_rejected_email_domain("first.example")
    monkeypatch.setattr(
        reg,
        "yyds_get_domains",
        lambda api_key=None, jwt=None: [
            {"domain": "first.example", "isVerified": True, "isPublic": True},
            {"domain": "second.example", "isVerified": True, "isPublic": True},
            {"domain": "third.example", "isVerified": True, "isPublic": True},
        ],
    )
    monkeypatch.setattr(reg, "_yyds_domain_index", 0, raising=False)

    assert reg.yyds_pick_domain() == "second.example"


def test_remember_rejected_email_domain_also_blocks_sibling_subdomains():
    reg._rejected_email_domains.clear()
    reg.remember_rejected_email_domain("007.hzeg.eu.org")

    assert reg.is_email_domain_rejected("007.hzeg.eu.org")
    assert reg.is_email_domain_rejected("10011.hzeg.eu.org")
    assert reg.is_email_domain_rejected("hzeg.eu.org")
    assert not reg.is_email_domain_rejected("10161993.xyz")


def test_list_registered_accounts_reads_accounts_files(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    tmp_path.joinpath("accounts_20260630_140000_job.txt").write_text(
        "user1@example.com----Pass-1----sso-token-1\n"
        "bad line\n"
        "user2@example.com----Pass-2----sso=sso-token-2----refresh-token-2\n",
        encoding="utf-8",
    )

    accounts = reg.list_registered_accounts()

    assert [item["email"] for item in accounts] == ["user1@example.com", "user2@example.com"]
    assert accounts[0]["password"] == "Pass-1"
    assert accounts[0]["sso_preview"] == "sso-to...oken-1"
    assert accounts[0]["sso"] == "sso-token-1"
    assert accounts[0]["has_refresh_token"] is False
    assert accounts[1]["sso"] == "sso-token-2"
    assert accounts[1]["has_refresh_token"] is True
    assert accounts[1]["refresh_token"] == "refresh-token-2"
    assert accounts[1]["refresh_token_preview"] == "refres...oken-2"
    assert accounts[0]["line_no"] == 1
    assert accounts[1]["line_no"] == 3


def test_list_registered_accounts_merges_persisted_sub2api_status(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    tmp_path.joinpath("accounts_20260630_140000_job.txt").write_text(
        "user@example.com----Pass----sso-token----refresh-token\n",
        encoding="utf-8",
    )
    account = reg.list_registered_accounts()[0]

    reg.persist_sub2api_push_status(
        [account],
        {"items": [{"email": account["email"], "response": {"id": 101, "name": "Grok Auto"}}]},
    )

    refreshed = reg.list_registered_accounts()[0]
    assert refreshed["sub2api_status"] == "pushed"
    assert refreshed["sub2api_status_text"] == "已推送"
    assert refreshed["sub2api_response"]["id"] == 101


def test_list_registered_accounts_merges_persisted_grok2api_status(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    tmp_path.joinpath("accounts_20260630_140000_job.txt").write_text(
        "user@example.com----Pass----sso-token----refresh-token\n",
        encoding="utf-8",
    )
    account = reg.list_registered_accounts()[0]

    reg.persist_grok2api_push_status(
        [account],
        {"items": [{"email": account["email"], "response": {"pool": "basic"}}]},
    )

    refreshed = reg.list_registered_accounts()[0]
    assert refreshed["grok2api_status"] == "pushed"
    assert refreshed["grok2api_status_text"] == "已推送"
    assert refreshed["grok2api_response"]["pool"] == "basic"


def test_account_push_statuses_do_not_overwrite_each_other(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    tmp_path.joinpath("accounts_20260630_140000_job.txt").write_text(
        "user@example.com----Pass----sso-token----refresh-token\n",
        encoding="utf-8",
    )
    account = reg.list_registered_accounts()[0]

    reg.persist_grok2api_push_status([account], {"items": [{"response": {"pool": "basic"}}]})
    reg.persist_sub2api_push_status([account], {"items": [{"response": {"id": 101}}]})

    refreshed = reg.list_registered_accounts()[0]
    assert refreshed["grok2api_status"] == "pushed"
    assert refreshed["sub2api_status"] == "pushed"
    assert refreshed["grok2api_response"]["pool"] == "basic"
    assert refreshed["sub2api_response"]["id"] == 101


def test_exchange_xai_refresh_token_posts_refresh_grant(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {"access_token": "access-token", "refresh_token": "rotated-refresh"}

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    monkeypatch.setattr(reg, "http_post", fake_post)

    result = reg.exchange_xai_refresh_token("refresh-token")

    assert result["access_token"] == "access-token"
    assert calls[0][0] == "https://auth.x.ai/oauth2/token"
    assert calls[0][1]["data"]["grant_type"] == "refresh_token"
    assert calls[0][1]["data"]["client_id"] == reg.XAI_GROK_OAUTH_CLIENT_ID
    assert calls[0][1]["data"]["refresh_token"] == "refresh-token"


def test_exchange_xai_refresh_token_includes_oauth_error_response(monkeypatch):
    class FakeResponse:
        status_code = 400
        text = '{"error":"invalid_grant","error_description":"Refresh token has been revoked"}'

        def raise_for_status(self):
            raise RuntimeError("HTTP Error 400")

    monkeypatch.setattr(reg, "http_post", lambda *args, **kwargs: FakeResponse())

    with pytest.raises(ValueError, match="invalid_grant.*revoked"):
        reg.exchange_xai_refresh_token("refresh-token")


def test_check_registered_accounts_health_updates_rotated_refresh_token(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    account_file = tmp_path.joinpath("accounts_20260630_140000_job.txt")
    account_file.write_text(
        "user@example.com----Pass----sso-token----old-refresh\n",
        encoding="utf-8",
    )
    account = reg.list_registered_accounts()[0]

    monkeypatch.setattr(
        reg,
        "exchange_xai_refresh_token",
        lambda refresh_token, settings=None: {
            "access_token": "access-token",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        },
    )

    result = reg.check_registered_accounts_health([account])
    reg.persist_account_health_status([account], result)

    assert result["healthy"] == 1
    assert result["failed"] == 0
    assert result["items"][0]["status"] == "healthy"
    assert "new-refresh" in account_file.read_text(encoding="utf-8")
    refreshed = reg.list_registered_accounts()[0]
    assert refreshed["health_status"] == "healthy"
    assert refreshed["health_status_text"] == "可用"


def test_check_registered_accounts_health_marks_missing_refresh_token(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    tmp_path.joinpath("accounts_20260630_140000_job.txt").write_text(
        "user@example.com----Pass----sso-token\n",
        encoding="utf-8",
    )
    account = reg.list_registered_accounts()[0]
    monkeypatch.setattr(reg, "exchange_xai_refresh_token", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not call xAI")))

    result = reg.check_registered_accounts_health([account])
    reg.persist_account_health_status([account], result)

    assert result["healthy"] == 0
    assert result["failed"] == 1
    assert result["items"][0]["status"] == "incomplete"
    refreshed = reg.list_registered_accounts()[0]
    assert refreshed["health_status"] == "incomplete"
    assert refreshed["health_status_text"] == "资料不完整"


def test_import_accounts_to_grok2api_posts_sso_tokens_to_admin_api(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200
        text = '{"ok":true}'

        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    monkeypatch.setattr(reg, "http_post", fake_post)

    result = reg.import_accounts_to_grok2api(
        [
            {"email": "user1@example.com", "sso": "sso-token-1"},
            {"email": "user2@example.com", "sso": "sso=sso-token-2"},
        ],
        {
            "grok2api_remote_base": "http://grok2api.example",
            "grok2api_remote_app_key": "app-key",
            "grok2api_pool_name": "ssoBasic",
        },
    )

    assert result["imported"] is True
    assert result["total"] == 2
    assert calls[0][0] == "http://grok2api.example/admin/api/tokens/add"
    assert calls[0][1]["params"]["app_key"] == "app-key"
    assert calls[0][1]["json"] == {
        "tokens": ["sso-token-1", "sso-token-2"],
        "pool": "basic",
        "tags": ["auto-register"],
    }


def test_add_token_to_grok2api_remote_pool_uses_admin_api(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            return None

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    monkeypatch.setattr(reg, "http_post", fake_post)
    monkeypatch.setitem(reg.config, "grok2api_remote_base", "http://grok2api.example")
    monkeypatch.setitem(reg.config, "grok2api_remote_app_key", "app-key")
    monkeypatch.setitem(reg.config, "grok2api_pool_name", "ssoSuper")

    assert reg.add_token_to_grok2api_remote_pool("sso=token-value") is True
    assert calls[0][0] == "http://grok2api.example/admin/api/tokens/add"
    assert calls[0][1]["json"]["pool"] == "super"


def test_import_accounts_to_sub2api_requires_refresh_token(monkeypatch):
    calls = []
    monkeypatch.setattr(reg, "http_post", lambda *args, **kwargs: calls.append((args, kwargs)))

    with pytest.raises(ValueError, match="缺少 refresh_token"):
        reg.import_accounts_to_sub2api(
            [{"email": "user1@example.com", "sso": "sso-token-1"}],
            {
                "sub2api_base": "https://sub2api.example/api/v1",
                "sub2api_admin_token": "admin-key",
            },
        )

    assert calls == []


def test_import_accounts_to_sub2api_posts_grok_refresh_token(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload
            self.status_code = 200
            self.text = "{}"

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/admin/grok/oauth/refresh-token"):
            refresh_token = kwargs["json"]["refresh_token"]
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "access_token": f"access-for-{refresh_token}",
                        "refresh_token": f"rotated-{refresh_token}",
                        "token_type": "Bearer",
                        "expires_at": 1790000000,
                        "email": kwargs["json"].get("email", ""),
                    },
                }
            )
        if url.endswith("/admin/accounts"):
            return FakeResponse({"code": 0, "data": {"id": 100 + len(calls)}})
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(reg, "http_post", fake_post)

    result = reg.import_accounts_to_sub2api(
        [
            {"email": "user1@example.com", "sso": "sso-token-1", "refresh_token": "refresh-token-1"},
            {"email": "user2@example.com", "sso": "sso=sso-token-2", "refresh_token": "refresh-token-2"},
        ],
        {
            "sub2api_base": "https://sub2api.example/api/v1",
            "sub2api_auth_mode": "x-api-key",
            "sub2api_admin_token": "admin-key",
            "sub2api_account_name": "Grok Auto",
            "sub2api_group_ids": "1, 2",
            "sub2api_concurrency": 5,
            "sub2api_priority": 40,
        },
    )

    assert result["imported"] is True
    assert result["total"] == 2
    assert calls[0][0] == "https://sub2api.example/api/v1/admin/grok/oauth/refresh-token"
    assert calls[0][1]["headers"]["x-api-key"] == "admin-key"
    assert calls[0][1]["json"]["refresh_token"] == "refresh-token-1"
    assert calls[1][0] == "https://sub2api.example/api/v1/admin/accounts"
    assert calls[1][1]["json"]["name"] == "Grok Auto - user1@example.com"
    assert calls[1][1]["json"]["platform"] == "grok"
    assert calls[1][1]["json"]["type"] == "oauth"
    assert calls[1][1]["json"]["credentials"]["access_token"] == "access-for-refresh-token-1"
    assert calls[1][1]["json"]["credentials"]["refresh_token"] == "rotated-refresh-token-1"
    assert calls[1][1]["json"]["credentials"]["token_type"] == "Bearer"
    assert calls[1][1]["json"]["credentials"]["expires_at"] == 1790000000
    assert calls[1][1]["json"]["credentials"]["client_id"]
    assert calls[1][1]["json"]["credentials"]["base_url"] == "https://api.x.ai/v1"
    assert calls[1][1]["json"]["group_ids"] == [1, 2]
    assert calls[1][1]["json"]["concurrency"] == 5
    assert calls[1][1]["json"]["priority"] == 40
    assert "sso-token-1" not in str(calls[1][1]["json"])


def test_import_accounts_to_sub2api_returns_per_account_failure(monkeypatch):
    class FakeHTTPError(Exception):
        def __init__(self):
            super().__init__("HTTP Error 502: Bad Gateway")
            self.response = type("Response", (), {"status_code": 502, "text": "Bad Gateway"})()

    class FakeResponse:
        status_code = 502
        text = "Bad Gateway"

        def raise_for_status(self):
            raise FakeHTTPError()

    monkeypatch.setattr(reg, "http_post", lambda *args, **kwargs: FakeResponse())

    result = reg.import_accounts_to_sub2api(
        [{"email": "user@example.com", "sso": "sso-token", "refresh_token": "refresh-token"}],
        {
            "sub2api_base": "https://sub2api.example/api/v1",
            "sub2api_admin_token": "admin-key",
        },
    )

    assert result["imported"] is False
    assert result["total"] == 0
    assert result["failed"] == 1
    assert result["items"][0]["status"] == "failed"
    assert result["items"][0]["step"] == "refresh-token"
    assert "HTTP 502" in result["items"][0]["error"]


def test_replace_registered_account_refresh_token_updates_source_file(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    account_file = tmp_path.joinpath("accounts_20260630_140000_job.txt")
    account_file.write_text(
        "user1@example.com----Pass-1----sso-token-1----old-refresh-token\n"
        "user2@example.com----Pass-2----sso-token-2----refresh-token-2\n",
        encoding="utf-8",
    )
    account = reg.list_registered_accounts()[0]

    assert reg.replace_registered_account_refresh_token(account, "new-refresh-token") is True

    assert account_file.read_text(encoding="utf-8").splitlines()[0] == (
        "user1@example.com----Pass-1----sso-token-1----new-refresh-token"
    )


def test_import_accounts_to_sub2api_reauths_when_refresh_token_revoked(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    tmp_path.joinpath("accounts_20260630_140000_job.txt").write_text(
        "user@example.com----Pass----sso-token----old-refresh-token\n",
        encoding="utf-8",
    )
    account = reg.list_registered_accounts()[0]
    calls = []

    class FakeHTTPError(Exception):
        def __init__(self):
            super().__init__("HTTP Error 502: Bad Gateway")
            self.response = type(
                "Response",
                (),
                {
                    "status_code": 502,
                    "text": '{"error":"invalid_grant","error_description":"Refresh token has been revoked"}',
                },
            )()

    class FakeResponse:
        def __init__(self, payload=None, fail=False):
            self.payload = payload or {}
            self.fail = fail
            self.status_code = 200
            self.text = "{}"

        def raise_for_status(self):
            if self.fail:
                raise FakeHTTPError()

        def json(self):
            return self.payload

    def fake_post(url, **kwargs):
        calls.append((url, kwargs["json"].get("refresh_token"), kwargs))
        if url.endswith("/admin/grok/oauth/refresh-token"):
            if kwargs["json"]["refresh_token"] == "old-refresh-token":
                return FakeResponse(fail=True)
            return FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "access_token": "access-token",
                        "refresh_token": "rotated-refresh-token",
                    },
                }
            )
        if url.endswith("/admin/accounts"):
            return FakeResponse({"code": 0, "data": {"id": 101}})
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(reg, "http_post", fake_post)
    monkeypatch.setattr(reg, "fetch_xai_oauth_refresh_token", lambda sso, log_callback=None, cancel_callback=None: "new-refresh-token")

    result = reg.import_accounts_to_sub2api(
        [account],
        {
            "sub2api_base": "https://sub2api.example/api/v1",
            "sub2api_admin_token": "admin-key",
        },
    )

    assert result["imported"] is True
    assert result["failed"] == 0
    assert [call[1] for call in calls if call[0].endswith("/admin/grok/oauth/refresh-token")] == [
        "old-refresh-token",
        "new-refresh-token",
    ]
    refreshed = reg.list_registered_accounts()[0]
    assert refreshed["refresh_token"] == "rotated-refresh-token"


def test_exchange_xai_oauth_code_for_token_posts_pkce_form(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {"access_token": "access-token", "refresh_token": "refresh-token"}

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    monkeypatch.setattr(reg, "http_post", fake_post)

    token_data = reg.exchange_xai_oauth_code_for_token("auth-code", "verifier")

    assert token_data["refresh_token"] == "refresh-token"
    assert calls[0][0] == "https://auth.x.ai/oauth2/token"
    assert calls[0][1]["data"]["grant_type"] == "authorization_code"
    assert calls[0][1]["data"]["client_id"] == reg.XAI_GROK_OAUTH_CLIENT_ID
    assert calls[0][1]["data"]["code"] == "auth-code"
    assert calls[0][1]["data"]["code_verifier"] == "verifier"
    assert calls[0][1]["data"]["redirect_uri"] == reg.XAI_GROK_OAUTH_REDIRECT_URI


def test_xai_oauth_consent_click_script_has_deep_fallbacks():
    script = reg.build_xai_oauth_consent_click_script()

    assert "if (!isConsentPage)" in script
    assert "shadowRoot" in script
    assert "querySelectorAll('*')" in script
    assert "form.submit()" in script
    assert "requestSubmit" in script
    assert "buttonDiagnostics" in script
    assert "buttons[buttons.length - 1]" not in script
    assert "centerX" in script
    assert "centerY" in script
    assert "oauth2/consent" in script


def test_click_xai_oauth_consent_uses_cdp_mouse_events():
    events = []

    class FakePage:
        def run_js(self, script):
            return {"clicked": False, "centerX": 123, "centerY": 456}

        def run_cdp(self, method, **kwargs):
            events.append((method, kwargs))

    result = reg._click_xai_oauth_consent_if_present(FakePage())

    assert result["nativeClicked"] is True
    assert [item[0] for item in events[:3]] == [
        "Input.dispatchMouseEvent",
        "Input.dispatchMouseEvent",
        "Input.dispatchMouseEvent",
    ]
    assert [item[0] for item in events[3:]] == ["Input.dispatchKeyEvent"] * 4
    assert events[0][1]["x"] == 123
    assert events[0][1]["y"] == 456


def test_fetch_xai_oauth_refresh_token_waits_after_first_consent_submit(monkeypatch):
    now = [1000.0]
    clicks = []
    sleeps = []
    logs = []

    class FakePage:
        def __init__(self):
            self.url = "https://accounts.x.ai/oauth2/consent?state=fixed-state"

        def run_cdp(self, method, **kwargs):
            return None

        def get(self, url):
            self.url = "https://accounts.x.ai/oauth2/consent?state=fixed-state"

    class FakeBrowser:
        def __init__(self):
            self.page = FakePage()

        def new_tab(self, url):
            return self.page

    def fake_click(page):
        clicks.append(now[0])
        return {"clicked": True, "submitted": True, "text": "allow", "isConsentPage": True}

    def fake_sleep(seconds, cancel_callback=None):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(reg.time, "time", lambda: now[0])
    monkeypatch.setattr(reg, "_get_browser", lambda: FakeBrowser())
    monkeypatch.setattr(reg, "_get_page", lambda: FakePage())
    monkeypatch.setattr(reg, "_set_page", lambda page: None)
    monkeypatch.setattr(reg, "_click_xai_oauth_consent_if_present", fake_click)
    monkeypatch.setattr(reg, "sleep_with_cancel", fake_sleep)
    monkeypatch.setattr(reg, "save_xai_oauth_debug_snapshot", lambda page, log_callback=None: [])
    monkeypatch.setattr(reg.secrets, "token_hex", lambda size: "fixed-state" if size == 32 else "fixed-nonce")
    monkeypatch.setattr(reg.secrets, "token_bytes", lambda size: b"a" * size)

    with pytest.raises(Exception, match="未在 3s 内返回 code"):
        reg.fetch_xai_oauth_refresh_token("sso-token", timeout=3, log_callback=logs.append)

    assert len(clicks) == 1
    assert any(seconds >= 1.0 for seconds in sleeps)


def test_fetch_xai_oauth_refresh_token_saves_debug_snapshot_on_timeout(monkeypatch, tmp_path):
    now = [1000.0]
    logs = []

    class FakePage:
        url = "https://accounts.x.ai/oauth2/consent?state=fixed-state"
        html = "<html><body>consent stuck</body></html>"

        def run_cdp(self, method, **kwargs):
            return None

        def get(self, url):
            self.url = "https://accounts.x.ai/oauth2/consent?state=fixed-state"

    class FakeBrowser:
        def __init__(self):
            self.page = FakePage()

        def new_tab(self, url):
            return self.page

    def fake_sleep(seconds, cancel_callback=None):
        now[0] += seconds

    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(reg.time, "time", lambda: now[0])
    monkeypatch.setattr(reg, "_get_browser", lambda: FakeBrowser())
    monkeypatch.setattr(reg, "_get_page", lambda: FakePage())
    monkeypatch.setattr(reg, "_set_page", lambda page: None)
    monkeypatch.setattr(reg, "_click_xai_oauth_consent_if_present", lambda page: {"clicked": False})
    monkeypatch.setattr(reg, "sleep_with_cancel", fake_sleep)
    monkeypatch.setattr(reg.secrets, "token_hex", lambda size: "fixed-state" if size == 32 else "fixed-nonce")
    monkeypatch.setattr(reg.secrets, "token_bytes", lambda size: b"a" * size)

    with pytest.raises(Exception, match="oauth_debug"):
        reg.fetch_xai_oauth_refresh_token("sso-token", timeout=1, log_callback=logs.append)

    debug_files = sorted(path.name for path in tmp_path.glob("oauth_debug_*"))
    assert any(name.endswith(".html") for name in debug_files)
    assert any("OAuth 调试快照" in line for line in logs)


def test_fetch_xai_oauth_refresh_token_sets_sso_cookies_before_authorize(monkeypatch):
    events = []

    class FakePage:
        def __init__(self):
            self.url = "https://127.0.0.1/callback?code=auth-code&state=fixed-state"

        def run_cdp(self, method, **kwargs):
            events.append(("cdp", method, kwargs))

        def get(self, url):
            events.append(("get", url))

        def run_js(self, script):
            return {"clicked": False}

    class FakeBrowser:
        def __init__(self):
            self.page = FakePage()

        def new_tab(self, url):
            events.append(("new_tab", url))
            return self.page

    monkeypatch.setattr(reg, "_get_browser", lambda: FakeBrowser())
    monkeypatch.setattr(reg, "_get_page", lambda: FakePage())
    monkeypatch.setattr(reg, "_set_page", lambda page: None)
    monkeypatch.setattr(reg.secrets, "token_hex", lambda size: "fixed-state" if size == 32 else "fixed-nonce")
    monkeypatch.setattr(reg.secrets, "token_bytes", lambda size: b"a" * size)
    monkeypatch.setattr(
        reg,
        "exchange_xai_oauth_code_for_token",
        lambda code, verifier, redirect_uri=None: {"refresh_token": "refresh-token"},
    )

    assert reg.fetch_xai_oauth_refresh_token("sso-token") == "refresh-token"
    cookie_names = [item[2].get("name") for item in events if item[0] == "cdp"]
    assert "sso" in cookie_names
    assert "sso-rw" in cookie_names
    assert any(item[0] == "get" and "oauth2/authorize" in item[1] for item in events)


def test_should_log_cloudflare_wait_throttles_repeated_same_length(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(reg.time, "time", lambda: now[0])
    state = {}

    assert reg.should_log_cloudflare_wait(state, "submit", "0") is True
    assert reg.should_log_cloudflare_wait(state, "submit", "0") is False
    now[0] += 4.9
    assert reg.should_log_cloudflare_wait(state, "submit", "0") is False
    now[0] += 0.2
    assert reg.should_log_cloudflare_wait(state, "submit", "0") is True
    assert reg.should_log_cloudflare_wait(state, "submit", "794") is True


def test_profile_submit_script_supports_role_button_and_aria_labels():
    script = reg.build_profile_submit_script("submit")

    assert '[role="button"]' in script
    assert "aria-label" in script
    assert "wait-cloudflare" in script
    # managed/flexible Turnstile 经常没有可见 iframe，禁止“无挑战提前提交”
    assert "submitted-no-challenge" not in script
    assert "ready-to-submit-no-challenge" not in script
    assert "requestSubmit" in script
    assert "continue" in reg.PROFILE_SUBMIT_KEYWORDS
    assert "create" in reg.PROFILE_SUBMIT_KEYWORDS

    diagnose_script = reg.build_profile_submit_script("diagnose")

    assert 'action = "diagnose"' in diagnose_script


def test_profile_submit_script_retries_xai_error_page():
    script = reg.build_profile_submit_script("retry_error")

    assert "There was an error loading this page" in script
    assert "An error occurred" in script
    assert "profile-error-retry-target" in script
    assert "profile-error-page-no-retry" in script


def test_profile_submit_script_recovers_signup_entry_page():
    script = reg.build_profile_submit_script("recover_entry")

    assert "Create your account" in script
    assert "Sign up with email" in script
    assert "profile-entry-email-target" in script
    assert "profile-entry-page-not-detected" in script


def test_profile_submit_script_waits_for_password_validation_before_submit():
    script = reg.build_profile_submit_script("submit")

    assert "ValidatePassword" in script
    assert "wait-password-validation" in script
    assert "performance.getEntriesByType('resource')" in script


def test_fill_profile_retries_xai_error_page_before_submit(monkeypatch):
    logs = []
    cdp_events = []

    class FakePage:
        def __init__(self):
            self.error_retry_attempted = False

        def run_js(self, script, *args):
            if 'action = "diagnose"' in script:
                return '{"turnstile": {}}'
            if 'action = "retry_error"' in script:
                if not self.error_retry_attempted:
                    self.error_retry_attempted = True
                    return {
                        "state": "profile-error-retry-target",
                        "centerX": 111,
                        "centerY": 222,
                        "text": "Retry",
                    }
                return "profile-error-page-not-detected"
            if 'action = "submit"' in script:
                return "submitted"
            return "profile-filled"

        def run_cdp(self, method, **kwargs):
            cdp_events.append((method, kwargs))

    page = FakePage()
    monkeypatch.setattr(reg, "_get_page", lambda: page)
    monkeypatch.setattr(reg, "refresh_active_page", lambda: page)
    monkeypatch.setattr(reg, "build_profile", lambda: ("Ada", "Lovelace", "secret"))
    monkeypatch.setattr(reg, "sleep_with_cancel", lambda seconds, cancel_callback=None: None)

    profile = reg.fill_profile_and_submit(timeout=5, log_callback=logs.append)

    assert profile == {"given_name": "Ada", "family_name": "Lovelace", "password": "secret"}
    assert any("最终注册页错误页，点击 Retry 重试 (1/4)" in line for line in logs)
    assert any(
        method == "Input.dispatchMouseEvent"
        and kwargs.get("type") == "mouseReleased"
        and kwargs.get("x") == 111
        and kwargs.get("y") == 222
        for method, kwargs in cdp_events
    )


def test_fill_profile_recovers_signup_entry_page_before_submit(monkeypatch):
    logs = []
    cdp_events = []

    class FakePage:
        def __init__(self):
            self.entry_recovered = False

        def run_js(self, script, *args):
            if 'action = "diagnose"' in script:
                return '{"turnstile": {}}'
            if 'action = "retry_error"' in script:
                return "profile-error-page-not-detected"
            if 'action = "recover_entry"' in script:
                if not self.entry_recovered:
                    self.entry_recovered = True
                    return {
                        "state": "profile-entry-email-target",
                        "centerX": 333,
                        "centerY": 444,
                        "text": "Sign up with email",
                    }
                return "profile-entry-page-not-detected"
            if 'action = "submit"' in script:
                return "submitted"
            return "profile-filled"

        def run_cdp(self, method, **kwargs):
            cdp_events.append((method, kwargs))

    page = FakePage()
    monkeypatch.setattr(reg, "_get_page", lambda: page)
    monkeypatch.setattr(reg, "refresh_active_page", lambda: page)
    monkeypatch.setattr(reg, "build_profile", lambda: ("Ada", "Lovelace", "secret"))
    monkeypatch.setattr(reg, "sleep_with_cancel", lambda seconds, cancel_callback=None: None)

    profile = reg.fill_profile_and_submit(timeout=5, log_callback=logs.append)

    assert profile == {"given_name": "Ada", "family_name": "Lovelace", "password": "secret"}
    assert any("最终注册页退回注册入口，点击邮箱注册恢复" in line for line in logs)
    assert any(
        method == "Input.dispatchMouseEvent"
        and kwargs.get("type") == "mouseReleased"
        and kwargs.get("x") == 333
        and kwargs.get("y") == 444
        for method, kwargs in cdp_events
    )


def test_fill_profile_waits_for_password_validation_before_submit(monkeypatch):
    logs = []
    submit_attempts = [0]

    class FakePage:
        def run_js(self, script, *args):
            if 'action = "diagnose"' in script:
                return '{"turnstile": {}}'
            if 'action = "retry_error"' in script:
                return "profile-error-page-not-detected"
            if 'action = "recover_entry"' in script:
                return "profile-entry-page-not-detected"
            if 'action = "submit"' in script:
                submit_attempts[0] += 1
                if submit_attempts[0] == 1:
                    return "wait-password-validation"
                return "submitted"
            return "profile-filled"

    page = FakePage()
    monkeypatch.setattr(reg, "_get_page", lambda: page)
    monkeypatch.setattr(reg, "refresh_active_page", lambda: page)
    monkeypatch.setattr(reg, "build_profile", lambda: ("Ada", "Lovelace", "secret"))
    monkeypatch.setattr(reg, "sleep_with_cancel", lambda seconds, cancel_callback=None: None)

    profile = reg.fill_profile_and_submit(timeout=5, log_callback=logs.append)

    assert profile == {"given_name": "Ada", "family_name": "Lovelace", "password": "secret"}
    assert submit_attempts[0] == 2
    assert any("等待 xAI 密码校验完成" in line for line in logs)


def test_wait_for_sso_cookie_final_page_can_submit_without_visible_turnstile():
    source = Path("grok_register_ttk.py").read_text(encoding="utf-8")
    content_script = Path("turnstilePatch/content.js").read_text(encoding="utf-8")
    page_hook = Path("turnstilePatch/pageHook.js").read_text(encoding="utf-8")
    manifest = Path("turnstilePatch/manifest.json").read_text(encoding="utf-8")

    assert "final-page-submit-target" in source
    assert "_dispatch_cdp_click(page, x, y, include_keyboard=False)" in source
    assert "executedWidgets" in source
    assert "__grokTurnstile" in source
    assert "completeyoursignup" in source
    assert "completesignup" in source
    assert "not-final-page:" in source
    assert "最后最终页状态" in source
    # managed/flexible 模式无可见 iframe 时也必须等 token，禁止空 token 提交
    assert "只要 token 未签发就必须等待" in source
    assert "int(retried.get(\"tokenLen\") or 0) >= 80" in source
    assert 'chrome.runtime.getURL("pageHook.js")' in content_script
    assert "window.__grokTurnstile" in page_hook
    assert '"web_accessible_resources": ["pageHook.js"]' in manifest


def test_final_page_executes_each_turnstile_widget_only_once():
    source = Path("grok_register_ttk.py").read_text(encoding="utf-8")
    page_hook = Path("turnstilePatch/pageHook.js").read_text(encoding="utf-8")

    assert "executedWidgetIds" in source
    assert "executedWidgetIds" in page_hook
    assert "final-page-wait-cf" in source


def test_turnstile_hook_records_terminal_challenge_callbacks():
    source = Path("grok_register_ttk.py").read_text(encoding="utf-8")
    page_hook = Path("turnstilePatch/pageHook.js").read_text(encoding="utf-8")

    assert '"error-callback"' in page_hook
    assert '"expired-callback"' in page_hook
    assert '"timeout-callback"' in page_hook
    assert "errors: Array.isArray(rawHook.errors)" in source


def test_turnstile_challenge_target_uses_a_real_cdp_click():
    events = []

    class FakePage:
        def run_js(self, script):
            assert "turnstile-challenge-target" in script
            return {"state": "turnstile-challenge-target", "x": 111, "y": 222}

        def run_cdp(self, method, **params):
            events.append((method, params))

    result = reg._click_turnstile_challenge_if_visible(FakePage())

    assert result["nativeClicked"] is True
    assert any(
        method == "Input.dispatchMouseEvent"
        and params.get("type") == "mousePressed"
        and params.get("x") == 111
        and params.get("y") == 222
        for method, params in events
    )


def test_wait_for_sso_cookie_uses_native_click_for_final_page(monkeypatch):
    events = []

    class FakePage:
        def run_js(self, script):
            return {
                "state": "final-page-submit-target",
                "centerX": 321,
                "centerY": 654,
                "text": "completesignup",
                # 只有 token 已签发时才允许原生点击提交
                "tokenLen": 120,
                "captured": {"hookInstalled": True, "widgets": [{"id": "widget-1"}]},
            }

        def run_cdp(self, method, **kwargs):
            events.append((method, kwargs))

        def cookies(self, all_domains=True, all_info=True):
            clicked = any(
                method == "Input.dispatchMouseEvent" and kwargs.get("type") == "mouseReleased"
                for method, kwargs in events
            )
            if clicked:
                return [{"name": "sso", "value": "sso-token"}]
            return []

    page = FakePage()
    monkeypatch.setattr(reg, "refresh_active_page", lambda: page)
    monkeypatch.setattr(reg, "_get_page", lambda: page)

    assert reg.wait_for_sso_cookie(timeout=1) == "sso-token"
    assert [item[0] for item in events[:3]] == [
        "Input.dispatchMouseEvent",
        "Input.dispatchMouseEvent",
        "Input.dispatchMouseEvent",
    ]
    assert events[0][1]["x"] == 321
    assert events[0][1]["y"] == 654


def test_yyds_code_polling_triggers_resend_callback(monkeypatch):
    now = [0.0]
    resend_calls = []

    monkeypatch.setattr(reg.time, "time", lambda: now[0])
    monkeypatch.setattr(reg, "yyds_get_messages", lambda address, token=None, jwt=None: [])

    def fake_sleep(seconds, cancel_callback=None):
        now[0] += 61

    monkeypatch.setattr(reg, "sleep_with_cancel", fake_sleep)

    with pytest.raises(Exception, match="未收到验证码"):
        reg.yyds_get_oai_code(
            "token",
            "user@example.com",
            timeout=120,
            resend_callback=lambda: resend_calls.append("resent"),
        )

    assert resend_calls


def test_create_browser_options_loads_turnstile_extension_and_user_agent(monkeypatch):
    recorded = {"args": [], "extension": None, "user_agent": None}

    class FakeOptions:
        def auto_port(self):
            return None

        def set_timeouts(self, base=1):
            return None

        def set_browser_path(self, path):
            return None

        def set_argument(self, *args):
            if len(args) == 1:
                recorded["args"].append(args[0])
            elif len(args) >= 2:
                recorded["args"].append(f"{args[0]}={args[1]}")

        def set_user_agent(self, value):
            recorded["user_agent"] = value

        def add_extension(self, path):
            recorded["extension"] = path

        def headless(self, value=True):
            return None

    monkeypatch.setattr(reg, "ChromiumOptions", FakeOptions)
    monkeypatch.setattr(reg, "config", {**reg.DEFAULT_CONFIG, "proxy": "", "user_agent": "TestAgent/1.0"})
    monkeypatch.setattr(reg, "should_apply_container_chrome_flags", lambda: True)
    monkeypatch.setattr(reg, "should_run_headless", lambda: False)
    monkeypatch.setattr(reg, "EXTENSION_PATH", "/tmp/fake-turnstile-extension")
    monkeypatch.setattr(reg.os.path, "isdir", lambda path: path == "/tmp/fake-turnstile-extension")

    options = reg.create_browser_options()

    assert isinstance(options, FakeOptions)
    assert recorded["user_agent"] == "TestAgent/1.0"
    assert recorded["extension"] == "/tmp/fake-turnstile-extension"
    assert any("AutomationControlled" in str(arg) for arg in recorded["args"])


def test_docker_starts_web_server_directly_and_keeps_xvfb_for_registration():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    assert 'GROK_REG_HEADLESS=0' in dockerfile
    assert '        xvfb \\' in dockerfile
    assert 'CMD ["uvicorn", "web_app:app", "--host", "0.0.0.0", "--port", "8787"]' in dockerfile
    assert 'xvfb-run' not in dockerfile
    assert 'GROK_REG_HEADLESS: "0"' in compose


def test_docker_workflow_publishes_amd64_and_arm64_images():
    workflow = Path(".github/workflows/docker-image.yml").read_text(encoding="utf-8")

    assert "docker/setup-qemu-action@v3" in workflow
    assert "platforms: linux/amd64,linux/arm64" in workflow


def test_loopback_proxy_is_rewritten_inside_docker(monkeypatch):
    monkeypatch.setenv("GROK_REG_IN_DOCKER", "1")

    assert (
        reg.normalize_proxy_for_runtime("http://127.0.0.1:7890")
        == "http://host.docker.internal:7890"
    )
    assert (
        reg.normalize_proxy_for_runtime("socks5://localhost:1080")
        == "socks5://host.docker.internal:1080"
    )


def test_browser_options_apply_configured_proxy(monkeypatch):
    class FakeOptions:
        def __init__(self):
            self.arguments = []

        def auto_port(self):
            return self

        def set_timeouts(self, base=1):
            self.base_timeout = base
            return self

        def set_argument(self, key, value=None):
            self.arguments.append((key, value))
            return self

        def add_extension(self, path):
            self.extension = path
            return self

    monkeypatch.setattr(reg, "ChromiumOptions", FakeOptions)
    monkeypatch.setenv("GROK_REG_IN_DOCKER", "1")
    monkeypatch.setitem(reg.config, "proxy", "http://127.0.0.1:7890")

    options = reg.create_browser_options()

    assert ("--proxy-server", "http://host.docker.internal:7890") in options.arguments
    assert not hasattr(options, "extension")


def test_turnstile_hook_is_deferred_until_profile_form():
    source = Path("grok_register_ttk.py").read_text(encoding="utf-8")
    browser_start = source.index("def start_browser(")
    browser_end = source.index("\ndef stop_browser", browser_start)
    signup_start = source.index("def open_signup_page(")
    signup_end = source.index("\ndef has_profile_form", signup_start)
    profile_start = source.index("def fill_profile_and_submit(")
    profile_end = source.index("\ndef wait_for_sso_cookie", profile_start)

    assert "install_turnstile_page_hook(" not in source[browser_start:browser_end]
    assert "install_turnstile_page_hook(" not in source[signup_start:signup_end]
    assert "install_turnstile_page_hook(page" in source[profile_start:profile_end]


def test_turnstile_hook_is_network_safe_and_installed_before_otp_submit():
    page_hook = Path("turnstilePatch/pageHook.js").read_text(encoding="utf-8")
    source = Path("grok_register_ttk.py").read_text(encoding="utf-8")
    otp_start = source.index("def fill_code_and_submit(")
    otp_end = source.index("\ndef getTurnstileToken", otp_start)

    assert "window.fetch =" not in page_hook
    assert "XMLHttpRequest" not in page_hook
    assert "install_turnstile_page_hook(page" in source[otp_start:otp_end]


def test_turnstile_page_hook_installs_with_cdp(monkeypatch):
    events = []

    class FakePage:
        def run_cdp(self, method, **kwargs):
            events.append((method, kwargs))

    monkeypatch.setattr(reg, "turnstile_page_hook_source", lambda: "window.__grokTurnstileHookInstalled = true;")

    assert reg.install_turnstile_page_hook(FakePage()) is True
    assert events[0][0] == "Page.addScriptToEvaluateOnNewDocument"
    assert events[0][1]["source"] == "window.__grokTurnstileHookInstalled = true;"
    assert events[1][0] == "Runtime.evaluate"
    assert events[1][1]["expression"] == "window.__grokTurnstileHookInstalled = true;"


def test_export_and_push_cpa_credential_writes_management_auth_file(monkeypatch, tmp_path):
    captured = {}

    class FakeMultipart:
        def __init__(self):
            self.parts = []
            self.closed = False

        def addpart(self, **kwargs):
            self.parts.append(kwargs)

        def close(self):
            self.closed = True

    class FakeResponse:
        status_code = 201
        text = '{"status":"ok"}'

        def raise_for_status(self):
            return None

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs["headers"]
        captured["multipart"] = kwargs["multipart"]
        captured["proxies"] = kwargs["proxies"]
        return FakeResponse()

    monkeypatch.setattr(reg, "http_post", fake_post)
    monkeypatch.setattr(reg, "CurlMime", FakeMultipart, raising=False)
    monkeypatch.setattr(
        reg,
        "exchange_xai_refresh_token",
        lambda token, settings=None: {
            "access_token": "access-token",
            "refresh_token": "rotated-refresh-token",
        },
    )

    result = reg.export_and_push_cpa_credential(
        "user@example.com",
        "refresh-token",
        {
            "cpa_auth_dir": str(tmp_path),
            "cpa_auto_push_remote": True,
            "cpa_management_base": "https://cpa.example.test/v0/management",
            "cpa_management_key": "management-secret",
        },
    )

    assert result["ok"] is True
    assert captured["url"] == "https://cpa.example.test/v0/management/auth-files"
    assert captured["headers"]["Authorization"] == "Bearer management-secret"
    assert captured["proxies"] == {}
    assert captured["multipart"].parts == [
        {
            "name": "file",
            "content_type": "application/json",
            "filename": "xai-user@example.com.json",
            "local_path": str(tmp_path / "xai-user@example.com.json"),
        }
    ]
    assert captured["multipart"].closed is True
    assert result["refresh_token"] == "rotated-refresh-token"
    payload = json.loads(tmp_path.joinpath(result["filename"]).read_text(encoding="utf-8"))
    assert payload["type"] == "xai"
    assert payload["refresh_token"] == "rotated-refresh-token"


def test_export_and_push_cpa_credential_keeps_local_file_when_upload_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(
        reg,
        "exchange_xai_refresh_token",
        lambda token, settings=None: {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
        },
    )
    monkeypatch.setattr(
        reg,
        "http_post",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("network down")),
    )

    result = reg.export_and_push_cpa_credential(
        "user@example.com",
        "refresh-token",
        {
            "cpa_auth_dir": str(tmp_path),
            "cpa_auto_push_remote": True,
            "cpa_management_base": "https://cpa.example.test",
            "cpa_management_key": "management-secret",
        },
    )

    assert result["ok"] is True
    assert result["upload_error"] == "network down"
    assert tmp_path.joinpath("xai-user@example.com.json").is_file()


def test_export_and_push_cpa_credential_derives_xai_metadata_from_access_token(monkeypatch, tmp_path):
    def encode_segment(value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    access_token = ".".join(
        [
            encode_segment({"alg": "none"}),
            encode_segment({"sub": "account-123", "iat": 1_700_000_000, "exp": 1_700_000_100}),
            "signature",
        ]
    )
    monkeypatch.setattr(
        reg,
        "exchange_xai_refresh_token",
        lambda token, settings=None: {"access_token": access_token, "refresh_token": "refresh-token"},
    )

    result = reg.export_and_push_cpa_credential(
        "user@example.com",
        "refresh-token",
        {"cpa_auth_dir": str(tmp_path)},
    )

    payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    assert payload["sub"] == "account-123"
    assert payload["expires_in"] == 100
    assert payload["expired"] == "2023-11-14T22:15:00Z"


def test_import_accounts_to_cpa_pushes_each_selected_account(monkeypatch):
    calls = []

    def fake_export(email, refresh_token, settings, log_callback=None):
        calls.append((email, refresh_token, settings))
        return {"ok": True, "uploaded": True, "filename": f"xai-{email}.json", "upload_status": 201}

    monkeypatch.setattr(reg, "export_and_push_cpa_credential", fake_export)
    accounts = [
        {"email": "user1@example.com", "refresh_token": "refresh-token-1"},
        {"email": "user2@example.com", "refresh_token": "refresh-token-2"},
    ]

    result = reg.import_accounts_to_cpa(
        accounts,
        {
            "cpa_auto_push_remote": False,
            "cpa_management_base": "https://cpa.example.test",
            "cpa_management_key": "management-secret",
        },
    )

    assert result["imported"] is True
    assert result["total"] == 2
    assert result["failed"] == 0
    assert [call[:2] for call in calls] == [
        ("user1@example.com", "refresh-token-1"),
        ("user2@example.com", "refresh-token-2"),
    ]
    assert all(call[2]["cpa_auto_push_remote"] is True for call in calls)


def test_import_accounts_to_cpa_pushes_normal_accounts_concurrently_and_keeps_order(monkeypatch):
    started = []
    lock = threading.Lock()
    second_push_started = threading.Event()

    def fake_export(email, refresh_token, settings, log_callback=None):
        with lock:
            started.append(email)
            if len(started) >= 2:
                second_push_started.set()
        assert second_push_started.wait(timeout=0.5), "CPA push did not run concurrently"
        return {"ok": True, "uploaded": True, "filename": f"xai-{email}.json"}

    monkeypatch.setattr(reg, "export_and_push_cpa_credential", fake_export)
    accounts = [
        {"email": "user1@example.com", "refresh_token": "refresh-token-1"},
        {"email": "user2@example.com", "refresh_token": "refresh-token-2"},
        {"email": "user3@example.com", "refresh_token": "refresh-token-3"},
    ]

    result = reg.import_accounts_to_cpa(
        accounts,
        {
            "cpa_management_base": "https://cpa.example.test",
            "cpa_management_key": "management-secret",
            "cpa_push_workers": 3,
        },
    )

    assert result["total"] == 3
    assert [item["email"] for item in result["items"]] == [
        "user1@example.com",
        "user2@example.com",
        "user3@example.com",
    ]


def test_import_accounts_to_cpa_keeps_pushing_after_an_account_failure(monkeypatch):
    calls = []

    def fake_export(email, refresh_token, settings, log_callback=None):
        calls.append(email)
        if email == "broken@example.com":
            return {"ok": True, "upload_error": "CPA unavailable"}
        return {"ok": True, "uploaded": True, "filename": f"xai-{email}.json"}

    monkeypatch.setattr(reg, "export_and_push_cpa_credential", fake_export)

    result = reg.import_accounts_to_cpa(
        [
            {"email": "broken@example.com", "refresh_token": "refresh-token-1"},
            {"email": "missing@example.com", "refresh_token": ""},
            {"email": "working@example.com", "refresh_token": "refresh-token-3"},
        ],
        {
            "cpa_management_base": "https://cpa.example.test",
            "cpa_management_key": "management-secret",
        },
    )

    assert calls == ["broken@example.com", "working@example.com"]
    assert result["imported"] is False
    assert result["total"] == 1
    assert result["failed"] == 2
    assert result["items"][0]["error"] == "CPA unavailable"
    assert result["items"][1]["error"] == "缺少 refresh_token"
    assert result["items"][2]["status"] == "pushed"


def test_import_accounts_to_cpa_reacquires_refresh_token_with_sso_after_http_400(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    source = tmp_path.joinpath("accounts_20260713_120000_job.txt")
    source.write_text(
        "user@example.com----Pass----sso-token----old-refresh-token\n",
        encoding="utf-8",
    )
    account = reg.list_registered_accounts()[0]
    calls = []

    class RefreshTokenError(Exception):
        def __init__(self):
            super().__init__("HTTP Error 400: ")
            self.code = 400

        def read(self):
            return b'{"error":"invalid_grant","error_description":"Refresh token has been revoked"}'

    def fake_export(email, refresh_token, settings, log_callback=None):
        calls.append(refresh_token)
        if refresh_token == "old-refresh-token":
            raise RefreshTokenError()
        return {
            "ok": True,
            "uploaded": True,
            "filename": "xai-user@example.com.json",
            "refresh_token": refresh_token,
        }

    monkeypatch.setattr(reg, "export_and_push_cpa_credential", fake_export)
    monkeypatch.setattr(
        reg,
        "fetch_xai_oauth_refresh_token",
        lambda sso, log_callback=None, cancel_callback=None: "new-refresh-token",
    )

    result = reg.import_accounts_to_cpa(
        [account],
        {
            "cpa_management_base": "https://cpa.example.test",
            "cpa_management_key": "management-secret",
        },
    )

    assert result["total"] == 1
    assert result["failed"] == 0
    assert calls == ["old-refresh-token", "new-refresh-token"]
    assert account["refresh_token"] == "new-refresh-token"
    assert source.read_text(encoding="utf-8").endswith("----new-refresh-token\n")


def test_import_accounts_to_cpa_persists_a_rotated_refresh_token(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    source = tmp_path.joinpath("accounts_20260713_120000_job.txt")
    source.write_text(
        "user@example.com----Pass----sso-token----old-refresh-token\n",
        encoding="utf-8",
    )
    account = reg.list_registered_accounts()[0]
    monkeypatch.setattr(
        reg,
        "export_and_push_cpa_credential",
        lambda *args, **kwargs: {
            "ok": True,
            "uploaded": True,
            "filename": "xai-user@example.com.json",
            "refresh_token": "rotated-refresh-token",
        },
    )

    result = reg.import_accounts_to_cpa(
        [account],
        {
            "cpa_management_base": "https://cpa.example.test",
            "cpa_management_key": "management-secret",
        },
    )

    assert result["total"] == 1
    assert account["refresh_token"] == "rotated-refresh-token"
    assert source.read_text(encoding="utf-8").endswith("----rotated-refresh-token\n")


def test_persist_cpa_push_status_is_visible_in_registered_accounts(monkeypatch, tmp_path):
    monkeypatch.setenv("GROK_REG_DATA_DIR", str(tmp_path))
    tmp_path.joinpath("accounts_20260713_120000_job.txt").write_text(
        "user@example.com----Pass----sso-token----refresh-token\n",
        encoding="utf-8",
    )
    account = reg.list_registered_accounts()[0]

    reg.persist_cpa_push_status(
        [account],
        {
            "items": [
                {
                    "email": account["email"],
                    "status": "pushed",
                    "response": {"filename": "xai-user@example.com.json", "upload_status": 201},
                }
            ]
        },
    )

    refreshed = reg.list_registered_accounts()[0]
    assert refreshed["cpa_status"] == "pushed"
    assert refreshed["cpa_status_text"] == "已推送"
    assert refreshed["cpa_response"]["upload_status"] == 201

    reg.persist_cpa_push_status(
        [account],
        {"items": [{"email": account["email"], "status": "failed", "error": "CPA unavailable"}]},
    )

    failed = reg.list_registered_accounts()[0]
    failed_record = reg.load_account_statuses()[account["id"]]
    assert failed["cpa_status"] == "failed"
    assert "cpa_response" not in failed
    assert "cpa_pushed_at" not in failed
    assert "cpa_response" not in failed_record
    assert "cpa_pushed_at" not in failed_record

    reg.persist_cpa_push_status(
        [account],
        {"items": [{"email": account["email"], "status": "pushed", "response": {"upload_status": 201}}]},
    )

    repushed = reg.list_registered_accounts()[0]
    repushed_record = reg.load_account_statuses()[account["id"]]
    assert repushed["cpa_status"] == "pushed"
    assert "cpa_error" not in repushed
    assert "cpa_step" not in repushed
    assert "cpa_failed_at" not in repushed
    assert "cpa_error" not in repushed_record
    assert "cpa_step" not in repushed_record
    assert "cpa_failed_at" not in repushed_record


def test_docker_visible_browser_keeps_linux_startup_flags(monkeypatch):
    class FakeOptions:
        def __init__(self):
            self.arguments = []

        def auto_port(self):
            return self

        def set_timeouts(self, base=1):
            return self

        def set_argument(self, key, value=None):
            self.arguments.append((key, value))
            return self

        def add_extension(self, path):
            return self

    monkeypatch.setattr(reg, "ChromiumOptions", FakeOptions)
    monkeypatch.setenv("GROK_REG_IN_DOCKER", "1")
    monkeypatch.setenv("GROK_REG_HEADLESS", "0")

    options = reg.create_browser_options()

    assert ("--no-sandbox", None) in options.arguments
    assert ("--disable-dev-shm-usage", None) in options.arguments
    assert ("--disable-gpu", None) in options.arguments
    assert ("--window-size", "1365,900") in options.arguments


def test_docker_forces_visible_mode_even_if_legacy_headless_env_is_set(monkeypatch):
    monkeypatch.setenv("GROK_REG_IN_DOCKER", "1")
    monkeypatch.setenv("GROK_REG_HEADLESS", "1")
    monkeypatch.delenv("GROK_REG_ALLOW_HEADLESS", raising=False)

    assert reg.should_run_headless() is False


def test_headless_can_only_be_forced_with_explicit_override(monkeypatch):
    monkeypatch.setenv("GROK_REG_IN_DOCKER", "1")
    monkeypatch.setenv("GROK_REG_HEADLESS", "1")
    monkeypatch.setenv("GROK_REG_ALLOW_HEADLESS", "1")

    assert reg.should_run_headless() is True


def test_visible_docker_starts_xvfb_when_display_is_missing(monkeypatch):
    calls = []

    class FakeProcess:
        def poll(self):
            return None

    def fake_popen(args, stdout=None, stderr=None):
        calls.append(args)
        return FakeProcess()

    monkeypatch.setenv("GROK_REG_IN_DOCKER", "1")
    monkeypatch.setenv("GROK_REG_HEADLESS", "0")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(reg.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(reg, "_xvfb_process", None)

    started = reg.ensure_virtual_display()

    assert started is True
    assert calls == [["Xvfb", ":99", "-screen", "0", "1365x900x24", "-nolisten", "tcp"]]
    assert reg.os.environ["DISPLAY"] == ":99"


def test_web_form_exposes_yyds_credentials():
    html = Path("templates/index.html").read_text(encoding="utf-8")

    assert 'name="yyds_api_key"' in html
    assert 'name="yyds_jwt"' in html


def test_web_console_uses_tabs_and_grouped_configuration():
    html = Path("templates/index.html").read_text(encoding="utf-8")

    assert 'data-tab-target="register"' in html
    assert 'data-tab-target="accounts"' in html
    assert 'data-tab-target="logs"' in html
    assert 'id="tab-register"' in html
    assert 'id="tab-accounts"' in html
    assert 'id="tab-logs"' in html
    assert 'data-config-section="基础注册"' in html
    assert 'data-config-section="邮箱服务"' in html
    assert 'data-config-section="sub2api"' in html
    assert html.index('id="tab-accounts"') > html.index('</form>')


def test_web_console_places_menu_on_left_side():
    html = Path("templates/index.html").read_text(encoding="utf-8")
    css = Path("static/app.css").read_text(encoding="utf-8")

    assert 'class="app-layout"' in html
    assert 'class="side-nav"' in html
    assert 'class="main-panel"' in html
    assert html.index('class="side-nav"') < html.index('class="main-panel"')
    assert ".app-layout" in css
    assert "grid-template-columns: 176px minmax(0, 1fr)" in css
    assert ".side-nav" in css


def test_web_console_exposes_dashboard_tab():
    html = Path("templates/index.html").read_text(encoding="utf-8")
    css = Path("static/app.css").read_text(encoding="utf-8")
    js = Path("static/app.js").read_text(encoding="utf-8")

    assert 'data-tab-target="dashboard"' in html
    assert 'id="tab-dashboard"' in html
    assert 'id="dashboardTotalAccounts"' in html
    assert 'id="dashboardPipeline"' in html
    assert 'id="dashboardHealthMix"' in html
    assert 'id="dashboardSources"' in html
    assert "renderDashboard" in js
    assert "dashboardMetricValue" in js
    assert ".dashboard-hero" in css
    assert ".flow-step" in css


def test_web_console_exposes_grok2api_push_action():
    html = Path("templates/index.html").read_text(encoding="utf-8")
    js = Path("static/app.js").read_text(encoding="utf-8")

    assert 'id="importGrok2apiBtn"' in html
    assert 'label: "grok2api"' in js
    assert 'label: "sub2api"' in js
    assert "/api/accounts/import/grok2api" in js
    assert "accountGrok2apiPushStatus" in js
    assert "可推送到 grok2api" in js


def test_web_console_exposes_account_health_check_action():
    html = Path("templates/index.html").read_text(encoding="utf-8")
    js = Path("static/app.js").read_text(encoding="utf-8")

    assert 'id="checkHealthBtn"' in html
    assert 'label: "健康状态"' in js
    assert "accountHealthStatus" in js
    assert "/api/accounts/check-health" in js
    assert "健康检查" in js


def test_web_console_exposes_selected_account_cpa_push_action():
    html = Path("templates/index.html").read_text(encoding="utf-8")
    js = Path("static/app.js").read_text(encoding="utf-8")

    assert 'id="importCpaBtn"' in html
    assert 'label: "CPA"' in js
    assert "accountCpaPushStatus" in js
    assert "/api/accounts/import/cpa" in js
    assert "importSelectedToCpa" in js
    assert 'async function importSelectedToCpa() {\n  const accountIds = selectedAccountIds();' in js


def test_web_console_exposes_selected_account_delete_action():
    html = Path("templates/index.html").read_text(encoding="utf-8")
    js = Path("static/app.js").read_text(encoding="utf-8")

    assert 'id="deleteAccountsBtn"' in html
    assert "deleteSelectedAccounts" in js
    assert 'requestJson("/api/accounts", {' in js
    assert 'method: "DELETE"' in js


def test_web_console_exposes_auto_push_switches():
    html = Path("templates/index.html").read_text(encoding="utf-8")
    js = Path("static/app.js").read_text(encoding="utf-8")

    assert 'name="grok2api_auto_add_remote"' in html
    assert 'name="sub2api_auto_import_remote"' in html
    assert 'name="cpa_auto_push_remote"' in html
    assert 'name="cpa_management_key"' in html
    assert 'name="cpa_push_workers"' in html
    assert "data.grok2api_auto_add_remote" in js
    assert "data.sub2api_auto_import_remote" in js
    assert "data.cpa_auto_push_remote" in js
    assert "data.cpa_push_workers" in js


def test_web_console_exposes_account_table_controls():
    html = Path("templates/index.html").read_text(encoding="utf-8")
    js = Path("static/app.js").read_text(encoding="utf-8")

    assert 'id="selectPageAccounts"' in html
    assert 'id="accountPageSize"' in html
    assert 'id="accountColumnsPanel"' in html
    assert 'id="accountPagination"' in html
    assert 'id="accountsHead"' in html
    assert 'data-column-toggle' in js
    assert "localStorage" in js
    assert "grok-reg.accounts.table" in js
    assert "ACCOUNT_COLUMNS" in js
    assert "selectedAccountIdsSet" in js
    assert "renderPagination" in js


def test_web_console_displays_continuous_account_row_number():
    js = Path("static/app.js").read_text(encoding="utf-8")

    assert '{ key: "index", label: "序号" }' in js
    assert "accountCellValue(account, column.key, rowNumber)" in js
    assert "index: rowNumber" in js
    assert "line: account.line_no" not in js


def test_web_console_uses_roomier_operational_layout():
    css = Path("static/app.css").read_text(encoding="utf-8")

    assert "width: min(1680px, calc(100vw - 24px))" in css
    assert "grid-template-columns: 176px minmax(0, 1fr)" in css
    assert "grid-template-columns: minmax(0, 1fr) 220px" in css
    assert "grid-template-columns: repeat(auto-fit, minmax(240px, 1fr))" in css
    assert "grid-column: 1 / -1" in css
    assert "min-width: 1240px" in css
