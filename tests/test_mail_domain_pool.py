import mail_domain_pool as mdp


def setup_function():
    mdp.reset_runtime()


def test_parse_and_compose_subdomain():
    domains = mdp.parse_domain_list("a.com, b.net，c.org")
    assert domains == ["a.com", "b.net", "c.org"]
    addr = mdp.compose_email_address(
        "a.com",
        enable_sub_domains=True,
        sub_domain_level=2,
        local_part="user01",
    )
    assert addr.startswith("user01@")
    assert addr.endswith(".a.com")
    assert addr.count(".") >= 3


def test_pinpoint_burst_sticks_to_one_domain():
    settings = mdp.settings_from_config(
        {
            "mail_domains": "a.com,b.com,c.com",
            "enable_mail_domain_runtime_control": True,
            "mail_domain_pinpoint_burst": True,
            "mail_domain_prefer_low_failure": True,
            "enable_mail_domain_grouping": False,
        }
    )
    first = mdp.pick_main_domain(settings)
    second = mdp.pick_main_domain(settings)
    third = mdp.pick_main_domain(settings)
    assert first == second == third


def test_cooldown_skips_domain():
    settings = mdp.settings_from_config(
        {
            "mail_domains": "a.com,b.com",
            "enable_mail_domain_runtime_control": True,
            "mail_domain_pinpoint_burst": False,
            "mail_domain_prefer_low_failure": False,
            "mail_domain_fail_threshold": 2,
            "mail_domain_fail_cooldown_sec": 60,
            "mail_domain_failure_types": ["discarded_email"],
        }
    )
    mdp.record_failure("a.com", "discarded_email", settings)
    mdp.record_failure("a.com", "discarded_email", settings)
    assert mdp.is_domain_cooling("a.com")
    picked = {mdp.pick_main_domain(settings) for _ in range(6)}
    assert "a.com" not in picked
    assert "b.com" in picked


def test_grouping_round_robin_switches_groups():
    settings = mdp.settings_from_config(
        {
            "mail_domains": "a.com,b.com,c.com,d.com",
            "enable_mail_domain_runtime_control": True,
            "enable_mail_domain_grouping": True,
            "mail_domain_group_count": 2,
            "mail_domain_group_mode": "auto",
            "mail_domain_group_strategy": "round_robin",
            "mail_domain_pinpoint_burst": False,
            "mail_domain_prefer_low_failure": False,
        }
    )
    groups = mdp.effective_groups(settings)
    assert len(groups) == 2
    # auto: [a,c] [b,d]
    picks = [mdp.pick_main_domain(settings) for _ in range(4)]
    # 应覆盖两组
    assert set(picks) & set(groups[0])
    assert set(picks) & set(groups[1])


def test_disabled_domain_not_picked():
    settings = mdp.settings_from_config(
        {
            "mail_domains": "a.com,b.com",
            "disabled_mail_domains": "a.com",
            "enable_mail_domain_runtime_control": True,
            "mail_domain_pinpoint_burst": False,
            "mail_domain_prefer_low_failure": False,
        }
    )
    for _ in range(5):
        assert mdp.pick_main_domain(settings) == "b.com"


def test_main_domain_of_subdomain_email():
    assert mdp.main_domain_of("u@x.y.a.com", ["a.com", "b.com"]) == "a.com"


def test_runtime_summary_shape():
    settings = mdp.settings_from_config(
        {
            "mail_domains": "a.com,b.com",
            "enable_mail_domain_runtime_control": True,
        }
    )
    mdp.pick_main_domain(settings)
    summary = mdp.runtime_summary(settings)
    assert summary["total_count"] == 2
    assert "domains" in summary
    assert len(summary["domains"]) == 2
