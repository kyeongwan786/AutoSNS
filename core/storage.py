"""
대상 목록(targets.json)과 기능별 일일 카운트(daily_count.json) 관리.

targets.json 구조:
{
  "블로그ID": {
    "post": "https://blog.naver.com/블로그ID/글번호",
    "buddy":   {"status": "done", "detail": "...", "date": "2026-10-03"},
    "like":    {"status": "skipped", ...},
    "comment": {"status": "done", "detail": "단 댓글", ...}
  }
}
기능별 status 가 done / skipped 면 그 기능은 다시 하지 않습니다.
"""
from __future__ import annotations
import json
from datetime import date, datetime
from pathlib import Path

from core.config import BASE_DIR

TARGETS_FILE = BASE_DIR / "targets.json"
DAILY_FILE = BASE_DIR / "daily_count.json"
ACTIVITY_FILE = BASE_DIR / "activity_log.json"

FINAL = {"done", "skipped"}


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def add_activity(blog_id: str, feature: str, status: str, detail: str = "") -> None:
    """Append one real automation result for the UI's activity history."""
    rows = load_json(ACTIVITY_FILE, [])
    rows.append({
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "blog_id": blog_id,
        "feature": feature,
        "status": status,
        "detail": detail,
    })
    save_json(ACTIVITY_FILE, rows[-5000:])


# ---------------- 대상 목록 ----------------

class Store:
    def __init__(self):
        self.targets: dict = self._migrate(load_json(TARGETS_FILE, {}))
        self.save()

    @staticmethod
    def _migrate(targets: dict) -> dict:
        """예전 단일 파일 버전(status 하나)의 기록을 기능별 구조로 변환"""
        for info in targets.values():
            old = info.pop("status", None)
            reason = info.pop("reason", "")
            info.pop("engage", None)
            info.pop("message", None)
            d = info.pop("date", None)
            if old and old != "pending" and "buddy" not in info:
                info["buddy"] = {"status": old, "detail": reason, "date": d or ""}
        return targets

    def save(self) -> None:
        save_json(TARGETS_FILE, self.targets)

    def add(self, blog_id: str, post_url: str) -> bool:
        if blog_id in self.targets:
            return False
        self.targets[blog_id] = {"post": post_url}
        return True

    def is_finished(self, blog_id: str, feature: str) -> bool:
        return self.targets[blog_id].get(feature, {}).get("status") in FINAL

    def needs_work(self, blog_id: str, features: list[str]) -> bool:
        return any(not self.is_finished(blog_id, f) for f in features)

    def mark(self, blog_id: str, feature: str, status: str, detail: str = "") -> None:
        self.targets[blog_id][feature] = {
            "status": status, "detail": detail, "date": str(date.today()),
        }
        self.save()


# ---------------- 일일 카운트 ----------------

def _daily() -> dict:
    data = load_json(DAILY_FILE, {})
    # 예전 형식 {날짜: 숫자} → {날짜: {"buddy": 숫자}}
    for k, v in list(data.items()):
        if isinstance(v, int):
            data[k] = {"buddy": v}
    return data


def today_count(feature: str) -> int:
    return _daily().get(str(date.today()), {}).get(feature, 0)


def add_today(feature: str) -> None:
    data = _daily()
    day = data.setdefault(str(date.today()), {})
    day[feature] = day.get(feature, 0) + 1
    save_json(DAILY_FILE, data)
