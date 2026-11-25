"""Structured local troubleshooting casebook (`lessons.json`) stored beside `rules.json`.

A lesson captures reusable operational guidance (e.g., "-10009 means transient retry,
do not record into a permanent route") rather than raw pixel coordinates or dynamic
question data. Lessons staged during a paused batch are promoted only after the task
passes acceptance, and decay automatically after consecutive failures.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from .storage import file_lock, read_json, write_json


STRATEGIES = {
    "transient_retry",
    "bind_role",
    "register_interrupt",
    "explore_nav",
    "icon_template",
    "ask_user",
    "note",
}
FAILURE_LIMIT = 3


def _clean_short(value: object, *, limit: int) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).split()).strip()
    if len(text) > limit:
        raise ValueError(f"字段长度不能超过 {limit} 字符")
    return text


def validate_lesson(spec: dict) -> dict:
    from .goals import ROLES
    from .platforms import PLATFORMS

    if not isinstance(spec, dict):
        raise ValueError("经验记录必须为对象")
    note = _clean_short(spec.get("note"), limit=240)
    if len(note) < 2:
        raise ValueError("经验说明 note 至少需要 2 个字符")
    strategy = _clean_short(spec.get("strategy") or "note", limit=40)
    if strategy not in STRATEGIES:
        raise ValueError(f"未知经验策略 {strategy}；可选：{sorted(STRATEGIES)}")
    role = _clean_short(spec.get("role"), limit=40) or None
    if role is not None and role not in ROLES:
        raise ValueError(f"未知页面角色 {role}；可选：{sorted(ROLES)}")
    if role is not None and strategy not in {"bind_role", "note"}:
        raise ValueError("只有 bind_role 或 note 策略允许指定 role")
    entry = _clean_short(spec.get("entry"), limit=40)
    if entry and entry not in PLATFORMS:
        raise ValueError(f"未知入口 {entry}；可选：{sorted(PLATFORMS)}")
    package = _clean_short(spec.get("package"), limit=120)
    if not package and entry:
        package = PLATFORMS[entry].PACKAGE
    step = _clean_short(spec.get("step"), limit=80)
    activity = _clean_short(spec.get("activity"), limit=160)
    raw_matches = spec.get("match_texts") or ()
    if isinstance(raw_matches, str):
        raw_matches = [part for part in raw_matches.split(",") if part.strip()]
    if not isinstance(raw_matches, (list, tuple)):
        raise ValueError("match_texts 必须为字符串列表")
    match_texts: list[str] = []
    for item in raw_matches:
        cleaned = _clean_short(item, limit=60)
        if cleaned and cleaned not in match_texts:
            match_texts.append(cleaned)
    if len(match_texts) > 8:
        raise ValueError("单条经验最多包含 8 个匹配关键词")
    source = _clean_short(spec.get("source") or "user", limit=20)
    if source not in {"user", "agent", "auto"}:
        raise ValueError("经验来源仅支持 user/agent/auto")
    return {
        "entry": entry,
        "package": package,
        "step": step,
        "activity": activity,
        "match_texts": match_texts,
        "strategy": strategy,
        "role": role,
        "note": note,
        "source": source,
    }


def lesson_id(normalized: dict) -> str:
    payload = "|".join([
        normalized.get("entry", ""),
        normalized.get("package", ""),
        normalized.get("step", ""),
        normalized.get("activity", ""),
        normalized.get("strategy", ""),
        normalized.get("role") or "",
        ",".join(sorted(normalized.get("match_texts", ()))),
        normalized.get("note", ""),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


class LessonStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.file = self.root / "lessons.json"

    def all(self) -> dict:
        if not self.file.exists():
            return {"version": 1, "lessons": []}
        try:
            data = read_json(self.file)
        except (OSError, ValueError):
            return {"version": 1, "lessons": []}
        if not isinstance(data, dict) or not isinstance(data.get("lessons"), list):
            return {"version": 1, "lessons": []}
        return data

    def list(self, *, include_inactive: bool = False) -> list[dict]:
        items = self.all()["lessons"]
        return items if include_inactive else [item for item in items if item.get("active", True)]

    def add(self, spec: dict, *, source: str | None = None) -> dict:
        payload = {**spec, **({"source": source} if source is not None else {})}
        normalized = validate_lesson(payload)
        ident = lesson_id(normalized)
        with file_lock(self.root / ".lessons.lock"):
            data = self.all()
            now = time.time()
            for existing in data["lessons"]:
                if existing.get("id") == ident:
                    existing.update(normalized, active=True, failures=0, verified_at=now)
                    existing.pop("invalidated_at", None)
                    existing.pop("reason", None)
                    write_json(self.file, data)
                    return existing
            record = {
                "id": ident,
                "active": True,
                **normalized,
                "verified_at": now,
                "successes": 0,
                "failures": 0,
                "last_used_at": None,
            }
            data["lessons"].append(record)
            write_json(self.file, data)
            return record

    def disable(self, ident: str, *, reason: str = "手动停用") -> dict:
        with file_lock(self.root / ".lessons.lock"):
            data = self.all()
            for item in data["lessons"]:
                if item.get("id") == ident:
                    item.update(active=False, invalidated_at=time.time(), reason=_clean_short(reason, limit=120) or "手动停用")
                    write_json(self.file, data)
                    return item
            raise ValueError(f"未找到经验记录：{ident}")

    def enable(self, ident: str) -> dict:
        with file_lock(self.root / ".lessons.lock"):
            data = self.all()
            for item in data["lessons"]:
                if item.get("id") == ident:
                    item.update(active=True, failures=0)
                    item.pop("invalidated_at", None)
                    item.pop("reason", None)
                    write_json(self.file, data)
                    return item
            raise ValueError(f"未找到经验记录：{ident}")

    def record_outcome(self, ident: str, *, success: bool, reason: str | None = None) -> dict | None:
        with file_lock(self.root / ".lessons.lock"):
            data = self.all()
            for item in data["lessons"]:
                if item.get("id") != ident:
                    continue
                now = time.time()
                if success:
                    item.update(active=True, failures=0, successes=item.get("successes", 0) + 1, last_used_at=now)
                    item.pop("invalidated_at", None)
                    item.pop("reason", None)
                else:
                    failures = item.get("failures", 0) + 1
                    item.update(failures=failures, last_failure_at=now, last_error=_clean_short(reason, limit=160) or None)
                    if failures >= FAILURE_LIMIT:
                        item.update(active=False, invalidated_at=now, reason=item["last_error"] or "连续失效")
                write_json(self.file, data)
                return item
            return None

    def recall(self, *, entry: str = "", package: str = "", step: str = "", activity: str = "",
               texts: tuple[str, ...] | list[str] = (), reason: str = "", limit: int = 3) -> list[dict]:
        """Return the top active lessons relevant to the current failure scene."""
        if limit < 1:
            return []
        corpus = "\n".join(str(part) for part in (reason, *texts) if part).casefold()
        scored: list[tuple[tuple, dict]] = []
        step_suffix = step.rsplit(".", 1)[-1] if "." in step else ""
        for item in self.list(include_inactive=False):
            if item.get("package") and package and item["package"] != package:
                continue
            if item.get("entry") and entry and item["entry"] != entry:
                continue
            score = 0
            item_step = item.get("step") or ""
            if item_step:
                if item_step == step:
                    score += 50
                elif step_suffix and "." in item_step and item_step.rsplit(".", 1)[-1] == step_suffix:
                    score += 20
                else:
                    continue
            else:
                score += 10
            item_activity = item.get("activity") or ""
            if item_activity:
                if item_activity == activity:
                    score += 20
                elif activity:
                    if not item_step and not item.get("match_texts"):
                        continue
                    score -= 10
            else:
                score += 5
            keywords = item.get("match_texts") or []
            matched = [kw for kw in keywords if kw.casefold() in corpus]
            if keywords and not matched:
                continue
            score += 30 * len(matched)
            if score <= 0:
                continue
            compact = {
                "id": item["id"],
                "strategy": item.get("strategy", "note"),
                "note": str(item.get("note", ""))[:180],
                "matched": matched,
                "successes": item.get("successes", 0),
            }
            if item.get("role"):
                compact["role"] = item["role"]
            rank = (-score, item.get("failures", 0), -item.get("successes", 0), -(item.get("verified_at") or 0))
            scored.append((rank, compact))
        scored.sort(key=lambda pair: pair[0])
        return [compact for _, compact in scored[:limit]]
