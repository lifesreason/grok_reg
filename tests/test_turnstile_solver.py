import time

import pytest

import grok_register_ttk as reg


def test_scrape_turnstile_sitekey_text_from_common_patterns():
    html = """
    <div class="cf-turnstile" data-sitekey="0x4AAAAAAAhr9JGVDZbrZOo0"></div>
    """
    assert reg.scrape_turnstile_sitekey_text(html) == "0x4AAAAAAAhr9JGVDZbrZOo0"

    js = 'sitekey: "0x4AAAAAAAhr9JGVDZbrZOo0", action: "signup"'
    assert reg.scrape_turnstile_sitekey_text(js) == "0x4AAAAAAAhr9JGVDZbrZOo0"

    assert reg.scrape_turnstile_sitekey_text("no key here") == ""


def test_validate_registration_config_normalizes_turnstile_solver_fields():
    settings = reg.validate_registration_config(
        {
            "email_provider": "duckmail",
            "turnstile_solver_enabled": "true",
            "turnstile_solver_fallback_click": "0",
            "turnstile_solver_url": "http://127.0.0.1:5072/",
            "turnstile_solver_client_key": "",
            "turnstile_solver_timeout": "999",
            "turnstile_sitekey": "",
            "turnstile_wait_seconds": "30",
        }
    )

    assert settings["turnstile_solver_enabled"] is True
    assert settings["turnstile_solver_fallback_click"] is False
    assert settings["turnstile_solver_url"] == "http://127.0.0.1:5072"
    assert settings["turnstile_solver_client_key"] == "local"
    assert settings["turnstile_solver_timeout"] == 300.0
    assert settings["turnstile_sitekey"].startswith("0x4")
    assert settings["turnstile_wait_seconds"] == 45.0


def test_normalize_turnstile_solver_url_maps_docker_loopback(monkeypatch):
    monkeypatch.setenv("GROK_REG_IN_DOCKER", "1")
    monkeypatch.delenv("GROK_REG_TURNSTILE_SOLVER_URL", raising=False)
    monkeypatch.setitem(reg.config, "turnstile_solver_url", "http://127.0.0.1:5072")

    assert reg.normalize_turnstile_solver_url() == "http://host.docker.internal:5072"

    monkeypatch.setenv("GROK_REG_TURNSTILE_SOLVER_URL", "http://127.0.0.1:5099")
    assert reg.normalize_turnstile_solver_url() == "http://host.docker.internal:5099"


def test_solve_turnstile_via_local_solver_polls_until_ready(monkeypatch):
    monkeypatch.setitem(reg.config, "turnstile_solver_url", "http://solver.test:5072")
    monkeypatch.setitem(reg.config, "turnstile_solver_client_key", "local")
    monkeypatch.setitem(reg.config, "turnstile_solver_timeout", 30)

    calls = []

    def fake_http(method, path, payload=None, timeout=20.0):
        calls.append((method, path, payload))
        if path == "/createTask":
            return {"errorId": 0, "taskId": "task-abc"}, 200
        if path == "/getTaskResult":
            # first poll processing, second ready
            if sum(1 for c in calls if c[1] == "/getTaskResult") < 2:
                return {"errorId": 0, "status": "processing"}, 200
            token = "t" * 100
            return {
                "errorId": 0,
                "status": "ready",
                "solution": {"token": token},
            }, 200
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr(reg, "_solver_http_json", fake_http)
    monkeypatch.setattr(reg, "sleep_with_cancel", lambda *_a, **_k: None)

    token = reg.solve_turnstile_via_local_solver(
        website_url="https://accounts.x.ai/sign-up",
        website_key="0x4AAAAAAAhr9JGVDZbrZOo0",
    )
    assert len(token) == 100
    assert calls[0][1] == "/createTask"
    assert calls[0][2]["task"]["type"] == "TurnstileTaskProxyless"
    assert any(c[1] == "/getTaskResult" for c in calls)


def test_get_turnstile_token_prefers_solver_then_returns(monkeypatch):
    class DummyPage:
        pass

    page = DummyPage()
    monkeypatch.setattr(reg, "_get_page", lambda: page)
    monkeypatch.setattr(reg, "probe_local_turnstile_solver", lambda force=False, timeout=2.0: True)
    monkeypatch.setattr(
        reg,
        "scrape_turnstile_context_from_page",
        lambda _p: {
            "url": "https://accounts.x.ai/sign-up",
            "sitekey": "0x4AAAAAAAhr9JGVDZbrZOo0",
            "action": "",
            "cdata": "",
            "source": "test",
        },
    )
    monkeypatch.setattr(
        reg,
        "solve_turnstile_via_local_solver",
        lambda **_k: "x" * 90,
    )
    monkeypatch.setattr(reg, "inject_turnstile_token_to_page", lambda _p, _t: 90)

    # 首次读空 → solver → inject 后再读到 token
    reads = {"n": 0}

    def read_token(_p):
        reads["n"] += 1
        return "" if reads["n"] == 1 else "x" * 90

    monkeypatch.setattr(reg, "_read_turnstile_token_from_page", read_token)
    monkeypatch.setitem(reg.config, "turnstile_solver_enabled", True)
    monkeypatch.setitem(reg.config, "turnstile_solver_fallback_click", True)
    reg._turnstile_solver_fail_until = 0.0

    token = reg.getTurnstileToken(attempts=1)
    assert token == "x" * 90


def test_get_turnstile_token_falls_back_to_click_when_solver_down(monkeypatch):
    class DummyPage:
        pass

    page = DummyPage()
    monkeypatch.setattr(reg, "_get_page", lambda: page)
    monkeypatch.setattr(reg, "probe_local_turnstile_solver", lambda force=False, timeout=2.0: False)

    state = {"n": 0}

    def read_token(_p):
        state["n"] += 1
        # first two empty, then success after click path
        return "y" * 90 if state["n"] >= 3 else ""

    monkeypatch.setattr(reg, "_read_turnstile_token_from_page", read_token)
    monkeypatch.setattr(
        reg,
        "_click_turnstile_via_shadow_dom",
        lambda _p, log_callback=None: {"ok": True, "actions": ["mock"]},
    )
    monkeypatch.setattr(reg, "sleep_with_cancel", lambda *_a, **_k: None)
    monkeypatch.setitem(reg.config, "turnstile_solver_enabled", True)
    monkeypatch.setitem(reg.config, "turnstile_solver_fallback_click", True)
    reg._turnstile_solver_fail_until = 0.0

    token = reg.getTurnstileToken(attempts=3)
    assert token == "y" * 90
