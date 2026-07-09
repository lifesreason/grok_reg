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
    assert reg.yyds_pick_domain() == "third.example"


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
    assert "submitted-no-challenge" in script
    assert "ready-to-submit-no-challenge" in script
    assert "requestSubmit" in script
    assert "continue" in reg.PROFILE_SUBMIT_KEYWORDS
    assert "create" in reg.PROFILE_SUBMIT_KEYWORDS

    diagnose_script = reg.build_profile_submit_script("diagnose")

    assert 'action = "diagnose"' in diagnose_script


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
    assert "const hasVisibleChallenge = !!document.querySelector('iframe[src*=\"turnstile\"], div.cf-turnstile, [data-sitekey]');" in source
    assert 'chrome.runtime.getURL("pageHook.js")' in content_script
    assert "window.__grokTurnstile" in page_hook
    assert '"web_accessible_resources": ["pageHook.js"]' in manifest


def test_wait_for_sso_cookie_uses_native_click_for_final_page(monkeypatch):
    events = []

    class FakePage:
        def run_js(self, script):
            return {
                "state": "final-page-submit-target",
                "centerX": 321,
                "centerY": 654,
                "text": "completesignup",
                "tokenLen": 0,
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


def test_docker_runs_visible_chromium_under_xvfb_by_default():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    assert 'GROK_REG_HEADLESS=0' in dockerfile
    assert 'xvfb-run' in dockerfile
    assert 'GROK_REG_HEADLESS: "0"' in compose


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


def test_web_console_exposes_auto_push_switches():
    html = Path("templates/index.html").read_text(encoding="utf-8")
    js = Path("static/app.js").read_text(encoding="utf-8")

    assert 'name="grok2api_auto_add_remote"' in html
    assert 'name="sub2api_auto_import_remote"' in html
    assert "data.grok2api_auto_add_remote" in js
    assert "data.sub2api_auto_import_remote" in js


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
