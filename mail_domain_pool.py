# -*- coding: utf-8 -*-
"""邮件域名内存池（对齐 openai-cpa mail_service 域名运行时控制）。

能力：
- 多级子域生成（摊薄 CF 主域日配额触发）
- 禁用主域列表
- 失败类型计数 + 阈值冷却
- 黄金矿工 / 定点爆破（pinpoint burst）
- 低失败优先
- 域名分组（round_robin / exhaust_then_next，auto/manual）
- 批次预分配
- 运行时统计快照
"""

from __future__ import annotations

import random
import string
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


FAILURE_TYPES = {
    "discarded_email",  # x.ai 拒收域名
    "cloudflare_temp_email_network",  # CF 建邮网络/API 失败
    "capacity_exceeded",  # 配额/容量
}

_LOCK = threading.RLock()
_STATE: Dict[str, Dict[str, Any]] = {}
_SESSION: Dict[str, Any] = {
    "counting_enabled": True,
    "tie_break_cursor": 0,
    "group_cursor": 0,
    "group_sticky_cursor": 0,
    "pinpoint_domain": "",
    "round_robin_cursor": 0,
}
_CONFIG_CACHE: Dict[str, Any] = {"key": None}


def _now() -> float:
    return time.time()


def normalize_domain(domain: str) -> str:
    text = str(domain or "").strip().lower().strip(".")
    if not text:
        return ""
    if "@" in text:
        text = text.rsplit("@", 1)[-1].strip().strip(".")
    return text


def parse_domain_list(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        parts = list(raw)
    else:
        text = str(raw).replace("，", ",").replace(";", ",").replace("\n", ",")
        parts = text.split(",")
    seen = set()
    out: List[str] = []
    for part in parts:
        d = normalize_domain(part)
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _parse_failure_types(raw: Any) -> Set[str]:
    if raw is None:
        return {"discarded_email"}
    if isinstance(raw, str):
        items = [x.strip().lower() for x in raw.replace("，", ",").split(",") if x.strip()]
    elif isinstance(raw, (list, tuple, set)):
        items = [str(x or "").strip().lower() for x in raw if str(x or "").strip()]
    else:
        items = ["discarded_email"]
    selected = {x for x in items if x in FAILURE_TYPES}
    return selected or {"discarded_email"}


def _bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "on"}
    if v is None:
        return default
    return bool(v)


def _int(v: Any, default: int, lo: int = 0, hi: int = 10**9) -> int:
    try:
        n = int(v)
    except Exception:
        n = default
    return max(lo, min(hi, n))


def settings_from_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """从 grok_reg config dict 提取池配置。"""
    main = parse_domain_list(config.get("mail_domains") or config.get("defaultDomains") or "")
    disabled = parse_domain_list(config.get("disabled_mail_domains") or [])
    # 仅保留主域池内的禁用项
    main_set = set(main)
    disabled = [d for d in disabled if d in main_set]
    groups_raw = config.get("mail_domain_groups") or []
    if isinstance(groups_raw, str):
        groups_raw = [groups_raw]
    if not isinstance(groups_raw, list):
        groups_raw = []
    groups = [",".join(parse_domain_list(g)) for g in groups_raw]
    group_count = _int(config.get("mail_domain_group_count"), 2, 1, 10)
    while len(groups) < group_count:
        groups.append("")
    groups = groups[:group_count]

    group_mode = str(config.get("mail_domain_group_mode") or "auto").strip().lower()
    if group_mode not in {"auto", "manual"}:
        group_mode = "auto"
    group_strategy = str(config.get("mail_domain_group_strategy") or "round_robin").strip().lower()
    if group_strategy not in {"round_robin", "exhaust_then_next"}:
        group_strategy = "round_robin"

    enable_runtime = _bool(config.get("enable_mail_domain_runtime_control"), True)
    enable_grouping = _bool(config.get("enable_mail_domain_grouping"), False) and enable_runtime
    pinpoint = _bool(config.get("mail_domain_pinpoint_burst"), False) and enable_runtime
    low_fail = _bool(config.get("mail_domain_prefer_low_failure"), True) and enable_runtime
    # 互斥：分组优先于定点；定点优先于低失败（与 cpa 一致）
    if enable_grouping:
        pinpoint = False
    if pinpoint and low_fail:
        low_fail = False

    return {
        "main_domains": main,
        "disabled_mail_domains": disabled,
        "enable_runtime": enable_runtime,
        "enable_sub_domains": _bool(config.get("enable_sub_domains"), False),
        "sub_domain_level": _int(config.get("sub_domain_level"), 1, 1, 7),
        "random_sub_domain_level": _bool(config.get("random_sub_domain_level"), False),
        "enable_grouping": enable_grouping,
        "group_count": group_count,
        "group_mode": group_mode,
        "group_strategy": group_strategy,
        "groups": groups,
        "pinpoint": pinpoint,
        "low_failure": low_fail,
        "failure_types": sorted(_parse_failure_types(config.get("mail_domain_failure_types"))),
        "fail_threshold": _int(config.get("mail_domain_fail_threshold"), 3, 0, 50),
        "fail_cooldown_sec": _int(config.get("mail_domain_fail_cooldown_sec"), 600, 0, 86400),
    }


def _new_state() -> Dict[str, Any]:
    return {
        "fail_count": 0,
        "success_count": 0,
        "pick_count": 0,
        "failure_counts": {},
        "last_failure_reason": "",
        "cooldown_until": 0.0,
        "cooldown_reason": "",
        "last_used_at": 0.0,
        "last_failure_at": 0.0,
        "last_success_at": 0.0,
    }


def _prune(now: Optional[float] = None) -> None:
    now = now if now is not None else _now()
    expired = [
        d
        for d, st in _STATE.items()
        if float(st.get("cooldown_until") or 0) > 0 and float(st.get("cooldown_until") or 0) <= now
    ]
    for d in expired:
        st = _STATE.get(d) or {}
        st["cooldown_until"] = 0.0
        st["cooldown_reason"] = ""
        st["fail_count"] = 0


def start_tracking() -> None:
    with _LOCK:
        _SESSION["counting_enabled"] = True


def stop_tracking() -> None:
    with _LOCK:
        _SESSION["counting_enabled"] = False


def reset_runtime() -> None:
    with _LOCK:
        _STATE.clear()
        _SESSION.update(
            {
                "counting_enabled": True,
                "tie_break_cursor": 0,
                "group_cursor": 0,
                "group_sticky_cursor": 0,
                "pinpoint_domain": "",
                "round_robin_cursor": 0,
            }
        )
        _CONFIG_CACHE["key"] = None


def main_domain_of(email_or_domain: str, main_domains: Sequence[str]) -> str:
    text = normalize_domain(email_or_domain)
    if "@" in str(email_or_domain or ""):
        text = normalize_domain(str(email_or_domain).rsplit("@", 1)[-1])
    roots = [normalize_domain(d) for d in main_domains if normalize_domain(d)]
    for root in roots:
        if text == root or text.endswith("." + root):
            return root
    return text if not roots else ""


def _state(domain: str) -> Dict[str, Any]:
    d = normalize_domain(domain)
    return _STATE.setdefault(d, _new_state())


def _recalc_fail(state: Dict[str, Any], selected: Set[str]) -> int:
    counts = state.get("failure_counts")
    if not isinstance(counts, dict):
        counts = {}
        state["failure_counts"] = counts
    total = sum(max(0, int(counts.get(r) or 0)) for r in selected)
    state["fail_count"] = total
    return total


def _build_auto_groups(main_domains: List[str], group_count: int) -> List[List[str]]:
    if not main_domains or group_count <= 0:
        return []
    group_count = min(group_count, len(main_domains))
    groups: List[List[str]] = [[] for _ in range(group_count)]
    for i, d in enumerate(main_domains):
        groups[i % group_count].append(d)
    return [g for g in groups if g]


def _build_manual_groups(main_domains: List[str], raw_groups: List[str]) -> List[List[str]]:
    master = set(main_domains)
    assigned = set()
    groups: List[List[str]] = []
    for raw in raw_groups:
        group = []
        for d in parse_domain_list(raw):
            if d in master and d not in assigned:
                assigned.add(d)
                group.append(d)
        if group:
            groups.append(group)
    # 未分配的主域挂到最后一组，避免丢失
    rest = [d for d in main_domains if d not in assigned]
    if rest:
        if groups:
            groups[-1].extend(rest)
        else:
            groups.append(rest)
    return groups


def effective_groups(settings: Dict[str, Any]) -> List[List[str]]:
    main = list(settings.get("main_domains") or [])
    if not main:
        return []
    if not settings.get("enable_grouping"):
        return [list(main)]
    if settings.get("group_mode") == "manual":
        groups = _build_manual_groups(main, list(settings.get("groups") or []))
        return groups if groups else [list(main)]
    groups = _build_auto_groups(main, int(settings.get("group_count") or 2))
    return groups if groups else [list(main)]


def _available_candidates(
    domains: Sequence[str],
    *,
    disabled: Set[str],
    rejected: Set[str],
    now: float,
) -> List[str]:
    out = []
    for d in domains:
        nd = normalize_domain(d)
        if not nd or nd in disabled:
            continue
        if nd in rejected or any(nd.endswith("." + r) for r in rejected if r):
            continue
        st = _state(nd)
        if float(st.get("cooldown_until") or 0) > now:
            continue
        out.append(nd)
    return out


def _select_low_failure(candidates: List[str], selected_types: Set[str]) -> str:
    best_key = None
    best_list: List[str] = []
    prioritized_clean = True
    for d in candidates:
        st = _state(d)
        fail = _recalc_fail(st, selected_types)
        clean = fail <= 0
        key = (int(st.get("pick_count") or 0), float(st.get("last_used_at") or 0))
        if best_key is None:
            prioritized_clean = clean
            best_key = key
            best_list = [d]
            continue
        if prioritized_clean and not clean:
            continue
        if clean and not prioritized_clean:
            prioritized_clean = True
            best_key = key
            best_list = [d]
            continue
        if key < best_key:
            best_key = key
            best_list = [d]
        elif key == best_key:
            best_list.append(d)
    if not best_list:
        return candidates[0]
    if len(best_list) == 1:
        return best_list[0]
    cur = int(_SESSION.get("tie_break_cursor") or 0)
    selected = best_list[cur % len(best_list)]
    _SESSION["tie_break_cursor"] = cur + 1
    return selected


def _mark_used(domain: str, now: float, inc: int = 1) -> str:
    st = _state(domain)
    st["last_used_at"] = now
    st["pick_count"] = max(0, int(st.get("pick_count") or 0)) + max(1, int(inc or 1))
    return domain


def _group_candidates_round_robin(groups: List[List[str]], disabled: Set[str], rejected: Set[str], now: float) -> List[str]:
    if not groups:
        return []
    cursor = int(_SESSION.get("group_cursor") or 0) % len(groups)
    for offset in range(len(groups)):
        idx = (cursor + offset) % len(groups)
        cands = _available_candidates(groups[idx], disabled=disabled, rejected=rejected, now=now)
        if cands:
            _SESSION["group_cursor"] = (idx + 1) % len(groups)
            return cands
    return []


def _group_candidates_exhaust(groups: List[List[str]], disabled: Set[str], rejected: Set[str], now: float) -> List[str]:
    if not groups:
        return []
    cursor = int(_SESSION.get("group_sticky_cursor") or 0) % len(groups)
    cur = _available_candidates(groups[cursor], disabled=disabled, rejected=rejected, now=now)
    if cur:
        _SESSION["group_sticky_cursor"] = cursor
        return cur
    for offset in range(1, len(groups) + 1):
        idx = (cursor + offset) % len(groups)
        cands = _available_candidates(groups[idx], disabled=disabled, rejected=rejected, now=now)
        if cands:
            _SESSION["group_sticky_cursor"] = idx
            return cands
    return []


def pick_main_domain(
    settings: Dict[str, Any],
    *,
    rejected: Optional[Set[str]] = None,
    now: Optional[float] = None,
) -> str:
    """选一个主域。"""
    now = now if now is not None else _now()
    rejected = {normalize_domain(x) for x in (rejected or set()) if normalize_domain(x)}
    main = list(settings.get("main_domains") or [])
    if not main:
        raise RuntimeError("mail_domains / defaultDomains 为空")
    disabled = set(settings.get("disabled_mail_domains") or [])

    with _LOCK:
        _prune(now)
        if not settings.get("enable_runtime"):
            cands = _available_candidates(main, disabled=disabled, rejected=rejected, now=now)
            if not cands:
                cands = [d for d in main if d not in disabled and d not in rejected]
            if not cands:
                raise RuntimeError("没有可用邮件主域")
            cur = int(_SESSION.get("round_robin_cursor") or 0)
            selected = cands[cur % len(cands)]
            _SESSION["round_robin_cursor"] = cur + 1
            return _mark_used(selected, now)

        # pinpoint / 黄金矿工
        if settings.get("pinpoint"):
            pin = normalize_domain(_SESSION.get("pinpoint_domain") or "")
            cands = _available_candidates(main, disabled=disabled, rejected=rejected, now=now)
            if pin and pin in cands:
                return _mark_used(pin, now)
            selected = cands[0] if cands else ""
            if not selected:
                # 全冷却：仍按顺序取第一个非禁用
                selected = next((d for d in main if d not in disabled and d not in rejected), "")
            if not selected:
                raise RuntimeError("没有可用邮件主域（均被禁用/拒收/冷却）")
            _SESSION["pinpoint_domain"] = selected
            return _mark_used(selected, now)

        groups = effective_groups(settings) if settings.get("enable_grouping") else []
        if groups:
            if settings.get("group_strategy") == "exhaust_then_next":
                cands = _group_candidates_exhaust(groups, disabled, rejected, now)
            else:
                cands = _group_candidates_round_robin(groups, disabled, rejected, now)
        else:
            cands = _available_candidates(main, disabled=disabled, rejected=rejected, now=now)

        if not cands:
            # 冷却耗尽时退回未禁用列表
            cands = [d for d in main if d not in disabled and d not in rejected]
        if not cands:
            raise RuntimeError("没有可用邮件主域（均被禁用/拒收/冷却）")

        if settings.get("low_failure"):
            selected = _select_low_failure(cands, set(settings.get("failure_types") or FAILURE_TYPES))
        else:
            cur = int(_SESSION.get("round_robin_cursor") or 0)
            # 保持主域配置顺序
            ordered = [d for d in main if d in set(cands)] or cands
            selected = ordered[cur % len(ordered)]
            _SESSION["round_robin_cursor"] = cur + 1
        return _mark_used(selected, now)


def preallocate_main_domains(
    settings: Dict[str, Any],
    batch_size: int,
    *,
    rejected: Optional[Set[str]] = None,
) -> List[Optional[str]]:
    """批量为 workers 预分配主域（黄金矿工时整批同一主域）。"""
    batch_size = max(0, int(batch_size or 0))
    if batch_size <= 0:
        return []
    now = _now()
    rejected = {normalize_domain(x) for x in (rejected or set()) if normalize_domain(x)}
    main = list(settings.get("main_domains") or [])
    disabled = set(settings.get("disabled_mail_domains") or [])

    with _LOCK:
        _prune(now)
        if settings.get("pinpoint") and settings.get("enable_runtime"):
            cands = _available_candidates(main, disabled=disabled, rejected=rejected, now=now)
            selected = cands[0] if cands else None
            if selected:
                _SESSION["pinpoint_domain"] = selected
                _mark_used(selected, now, inc=batch_size)
            return [selected] * batch_size

        out: List[Optional[str]] = []
        for _ in range(batch_size):
            try:
                out.append(pick_main_domain(settings, rejected=rejected, now=now))
            except Exception:
                out.append(None)
        return out


def record_failure(
    domain: str,
    reason: str,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    if not settings.get("enable_runtime") or not _SESSION.get("counting_enabled", True):
        return {}
    nd = normalize_domain(domain)
    # 若传入邮箱完整 host，映射主域
    if settings.get("main_domains"):
        root = main_domain_of(nd, settings["main_domains"])
        if root:
            nd = root
    reason = str(reason or "discarded_email").strip().lower()
    if reason not in FAILURE_TYPES:
        reason = "discarded_email"
    selected = set(settings.get("failure_types") or {"discarded_email"})
    threshold = int(settings.get("fail_threshold") or 0)
    cooldown_sec = int(settings.get("fail_cooldown_sec") or 0)
    now = _now()

    with _LOCK:
        _prune(now)
        st = _state(nd)
        counts = st.get("failure_counts")
        if not isinstance(counts, dict):
            counts = {}
            st["failure_counts"] = counts
        until = float(st.get("cooldown_until") or 0)
        st["last_failure_at"] = now
        st["last_failure_reason"] = reason
        if until > now:
            _recalc_fail(st, selected)
            st["fail_count"] = 0
            return {
                "domain": nd,
                "fail_count": 0,
                "cooled": False,
                "cooldown_until": until,
                "reason": reason,
                "already_cooling": True,
            }
        counts[reason] = int(counts.get(reason) or 0) + 1
        fail_count = _recalc_fail(st, selected)
        cooled = False
        if threshold > 0 and fail_count >= threshold and reason in selected:
            st["cooldown_until"] = now + max(0, cooldown_sec)
            st["cooldown_reason"] = reason
            st["fail_count"] = 0
            cooled = True
            if normalize_domain(_SESSION.get("pinpoint_domain") or "") == nd:
                _SESSION["pinpoint_domain"] = ""
        return {
            "domain": nd,
            "fail_count": int(st.get("fail_count") or 0),
            "cooled": cooled,
            "cooldown_until": float(st.get("cooldown_until") or 0),
            "reason": reason,
            "already_cooling": False,
        }


def record_success(domain: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    if not settings.get("enable_runtime") or not _SESSION.get("counting_enabled", True):
        return {}
    nd = normalize_domain(domain)
    if settings.get("main_domains"):
        root = main_domain_of(nd, settings["main_domains"])
        if root:
            nd = root
    selected = set(settings.get("failure_types") or {"discarded_email"})
    now = _now()
    with _LOCK:
        _prune(now)
        st = _state(nd)
        st["success_count"] = int(st.get("success_count") or 0) + 1
        st["last_success_at"] = now
        _recalc_fail(st, selected)
        return {
            "domain": nd,
            "success_count": int(st.get("success_count") or 0),
            "fail_count": int(st.get("fail_count") or 0),
        }


def set_disabled_domains(domains: Sequence[str], settings: Dict[str, Any]) -> List[str]:
    """返回规范化后的禁用列表（仅主域池内）。"""
    main_set = set(settings.get("main_domains") or [])
    disabled = []
    for d in parse_domain_list(domains):
        if d in main_set:
            disabled.append(d)
    return disabled


def clear_domain_counters(domain: str) -> Dict[str, Any]:
    nd = normalize_domain(domain)
    with _LOCK:
        st = _STATE.get(nd)
        if not st:
            return {}
        st["fail_count"] = 0
        st["failure_counts"] = {}
        st["cooldown_until"] = 0.0
        st["cooldown_reason"] = ""
        st["last_failure_reason"] = ""
        return {"domain": nd, "cleared": True}


def runtime_summary(settings: Dict[str, Any]) -> Dict[str, Any]:
    now = _now()
    main = list(settings.get("main_domains") or [])
    disabled = set(settings.get("disabled_mail_domains") or [])
    with _LOCK:
        _prune(now)
        cooling = 0
        available = 0
        rows = []
        for d in main:
            st = _state(d)
            until = float(st.get("cooldown_until") or 0)
            is_cool = until > now
            is_dis = d in disabled
            if is_cool:
                cooling += 1
            if not is_cool and not is_dis:
                available += 1
            rows.append(
                {
                    "domain": d,
                    "fail_count": int(st.get("fail_count") or 0),
                    "success_count": int(st.get("success_count") or 0),
                    "pick_count": int(st.get("pick_count") or 0),
                    "failure_counts": dict(st.get("failure_counts") or {}),
                    "last_failure_reason": st.get("last_failure_reason") or "",
                    "cooldown_until": until,
                    "cooldown_remaining_sec": max(0, int(until - now)) if is_cool else 0,
                    "cooldown_reason": st.get("cooldown_reason") or "",
                    "is_available": (not is_cool) and (not is_dis),
                    "is_disabled": is_dis,
                    "group": "",
                }
            )
        # 标注分组
        groups = effective_groups(settings)
        domain_group = {}
        for i, g in enumerate(groups):
            for d in g:
                domain_group[d] = i + 1
        for row in rows:
            gi = domain_group.get(row["domain"])
            row["group"] = f"[{gi}]" if gi else ""
        return {
            "total_count": len(main),
            "available_count": available,
            "cooldown_count": cooling,
            "disabled_count": len(disabled),
            "pinpoint_domain": normalize_domain(_SESSION.get("pinpoint_domain") or ""),
            "grouping": bool(settings.get("enable_grouping")),
            "domains": rows,
        }


def build_local_part(length: int = 10) -> str:
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choice(chars) for _ in range(max(4, int(length or 10))))


def build_subdomain_prefix(level: int = 1, random_level: bool = False, max_level: int = 4) -> str:
    if random_level:
        level = random.randint(1, max(1, min(7, int(max_level or 4))))
    else:
        level = max(1, min(7, int(level or 1)))
    parts = []
    for _ in range(level):
        parts.append(build_local_part(random.randint(4, 8)))
    return ".".join(parts)


def compose_email_address(
    main_domain: str,
    *,
    enable_sub_domains: bool = False,
    sub_domain_level: int = 1,
    random_sub_domain_level: bool = False,
    local_part: Optional[str] = None,
) -> str:
    main = normalize_domain(main_domain)
    if not main:
        raise ValueError("main_domain empty")
    local = (local_part or build_local_part(10)).strip().lower()
    if enable_sub_domains:
        host = f"{build_subdomain_prefix(sub_domain_level, random_sub_domain_level)}.{main}"
    else:
        host = main
    return f"{local}@{host}"


# 兼容旧 API 名
def parse_domain_list_compat(raw: Any) -> List[str]:
    return parse_domain_list(raw)


# 旧函数名兼容（grok_register_ttk 已引用）
def is_domain_cooling(domain: str, now: Optional[float] = None) -> bool:
    now = now if now is not None else _now()
    d = normalize_domain(domain)
    with _LOCK:
        st = _state(d)
        return float(st.get("cooldown_until") or 0) > now


def available_main_domains(
    main_domains: Sequence[str],
    *,
    rejected: Optional[Set[str]] = None,
    now: Optional[float] = None,
) -> List[str]:
    now = now if now is not None else _now()
    rejected = {normalize_domain(x) for x in (rejected or set()) if normalize_domain(x)}
    with _LOCK:
        _prune(now)
        return _available_candidates(main_domains, disabled=set(), rejected=rejected, now=now)


def snapshot(main_domains: Sequence[str] = ()) -> List[Dict[str, Any]]:
    settings = {"main_domains": list(main_domains or list(_STATE.keys())), "disabled_mail_domains": [], "enable_grouping": False}
    return runtime_summary(settings).get("domains") or []


# 旧 mark_* 兼容
def mark_domain_success(domain: str) -> None:
    record_success(domain, {"enable_runtime": True, "main_domains": [], "failure_types": list(FAILURE_TYPES)})


def mark_domain_failure(
    domain: str,
    *,
    reason: str = "discarded_email",
    threshold: int = 3,
    cooldown_sec: int = 600,
) -> Dict[str, Any]:
    return record_failure(
        domain,
        reason,
        {
            "enable_runtime": True,
            "main_domains": [],
            "failure_types": list(FAILURE_TYPES),
            "fail_threshold": threshold,
            "fail_cooldown_sec": cooldown_sec,
        },
    )
