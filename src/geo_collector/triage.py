"""Deterministic pre-AI failure triage for paused handoff scenes.

Separates scenes that require human intervention (login, captcha, risk control,
lockscreen) or simple transient retries from true UI drift so neither the host
Agent nor the bounded supervisor wastes calls guessing.
"""
from __future__ import annotations

import re


_AUTH_ACTIVITY = re.compile(
    r"(?:^|[.$_])(?:login|signin|auth|captcha|secverify|riskcontrol|accountlogin|passport)",
    re.IGNORECASE,
)
_AUTH_UI = re.compile(
    r"^(?:请先?|立即|一键|手机号|短信|密码|本机号码一键)?登录(?:注册)?$"
    r"|^获取验证码$"
    r"|^请输入(?:手机号|验证码)$"
    r"|^(?:安全|人机|滑动|拖动滑块|拼图)验证$"
    r"|^向右滑动(?:完成)?拼图$"
    r"|^请完成安全验证$"
    r"|^账号(?:异常|受限|已退出登录)$"
    r"|^滑动验证$"
)
_SYSTEM_PACKAGES = {
    "com.android.systemui",
    "com.miui.home",
    "com.huawei.android.launcher",
    "com.sec.android.app.launcher",
    "com.oppo.launcher",
    "com.bbk.launcher2",
}
_TRANSIENT_ENV = re.compile(
    r"-10009|网络(?:错误|异常|不给力|连接失败|不可用)|加载失败|请稍后重试|暂无网络|服务器开小差|请求超时|连接超时"
)
_RETRY_BUTTON = re.compile(r"^(?:点击|请|重新|刷新)?重试$|^重新加载$|^点击刷新$")
_DETECTOR_HINT = re.compile(r"接缝|原图像素|药图|药品图片|初始首项|视口|布局容器|缺页|未通过完整性")


def _short_labels(observation: dict, *, limit_len: int = 32) -> list[str]:
    package = observation.get("package", "")
    labels: list[str] = []
    seen: set[str] = set()
    for node in observation.get("nodes", []):
        if node.get("package", "") not in ("", None, package):
            continue
        for attr in ("text", "description"):
            raw = str(node.get(attr) or "").strip()
            if raw and len(raw) <= limit_len and raw not in seen:
                seen.add(raw)
                labels.append(raw)
    for line in observation.get("texts", []):
        if line.get("confidence", 0) < .78:
            continue
        raw = str(line.get("text") or "").strip()
        if raw and len(raw) <= limit_len and raw not in seen:
            seen.add(raw)
            labels.append(raw)
    return labels


def classify_failure(request: dict, observation: dict, candidates: list[dict] | None = None, *, uncertain: bool = False) -> dict:
    """Classify a paused scene into user_action_required, transient_env, detector_gap, or ui_drift."""
    step = str(request.get("step") or "")
    activity = str(observation.get("activity") or "")
    package = str(observation.get("package") or "")
    expected_package = str(request.get("package") or "")
    reason = str(request.get("reason") or "")
    missing = [str(item) for item in (request.get("missing_evidence") or []) if item]
    short_labels = _short_labels(observation, limit_len=32)

    # 1. User-action-required blockers (login / captcha / risk-control / system lockscreen).
    auth_signals: list[str] = []
    if expected_package and package and package != expected_package and package in _SYSTEM_PACKAGES:
        auth_signals.append(f"system_foreground:{package}")
    if activity and _AUTH_ACTIVITY.search(activity):
        auth_signals.append(f"auth_activity:{activity.rsplit('.', 1)[-1]}")
    for label in short_labels:
        if len(label) <= 24 and _AUTH_UI.fullmatch(label):
            auth_signals.append(f"auth_ui:{label}")
    if auth_signals:
        return {
            "failure_class": "user_action_required",
            "reason": "检测到登录、验证码、风控或系统桌面/锁屏界面，需人工在手机处理后再恢复原批次",
            "recommended_action": "ask_user",
            "signals": auth_signals[:4],
            "skip_ai": True,
        }

    # 2. Transient environment / network errors / pending page loads.
    transient_signals: list[str] = []
    if step.endswith(".results_ready"):
        transient_signals.append("step:results_ready")
    for label in short_labels:
        if _TRANSIENT_ENV.search(label):
            transient_signals.append(f"env:{label[:24]}")
        elif len(label) <= 12 and _RETRY_BUTTON.fullmatch(label):
            transient_signals.append(f"retry:{label}")
    if transient_signals:
        retry_candidate = None
        if not request.get("readonly") and not uncertain and candidates:
            for item in candidates:
                label = str(item.get("label") or "").strip()
                if label and len(label) <= 12 and _RETRY_BUTTON.fullmatch(label):
                    retry_candidate = item["id"]
                    break
        result = {
            "failure_class": "transient_env",
            "reason": "检测到网络异常、加载未就绪或临时重试提示；优先等待后 resume 或使用 transient 点击重试，不写入永久路径",
            "recommended_action": "transient_retry_or_wait",
            "signals": transient_signals[:4],
            "skip_ai": False,
        }
        if retry_candidate:
            result["auto_retry_candidate"] = retry_candidate
        return result

    # 3. Capture / seam / product image / layout detector boundaries.
    diagnostic_text = " ".join([reason, *missing])
    if step in {"capture.review", "products.images_wait"} or step.startswith("layout."):
        if _DETECTOR_HINT.search(diagnostic_text) or step.startswith("layout."):
            return {
                "failure_class": "detector_gap",
                "reason": "当前为采集完整性、药图就绪或布局容器复查；按 allowed_resolutions 绑定页面角色、执行有日志恢复，或检查检测器覆盖范围",
                "recommended_action": "inspect_or_code_fix",
                "signals": [f"step:{step}"],
                "skip_ai": False,
            }

    # 4. Default: actionable UI / navigation / role drift.
    return {
        "failure_class": "ui_drift",
        "reason": "页面控件或导航结构与现有规则不匹配，适合通过自愈探索、视觉模板、中断规则或角色绑定适配",
        "recommended_action": "self_heal",
        "signals": [f"step:{step}"] if step else [],
        "skip_ai": False,
    }
