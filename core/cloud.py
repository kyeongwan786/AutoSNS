"""Public configuration and API calls for Supabase and GitHub Releases."""
from __future__ import annotations

import json
import base64
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from functools import lru_cache

from core.config import RESOURCE_DIR


class CloudError(RuntimeError):
    pass


_ACCESS_TOKEN = os.environ.get("AUTOSNS_SUPABASE_ACCESS_TOKEN", "")
_REFRESH_TOKEN = os.environ.get("AUTOSNS_SUPABASE_REFRESH_TOKEN", "")


@lru_cache(maxsize=1)
def settings() -> dict:
    try:
        value = json.loads((RESOURCE_DIR / "cloud_settings.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def supabase_configured() -> bool:
    cfg = settings()
    return (str(cfg.get("supabase_url", "")).startswith("https://")
            and "YOUR_PROJECT" not in str(cfg.get("supabase_url", ""))
            and bool(cfg.get("supabase_anon_key"))
            and "REPLACE_" not in str(cfg.get("supabase_anon_key", "")))


def _request(url: str, method: str = "GET", payload: dict | None = None,
             access_token: str | None = None, extra_headers: dict | None = None):
    cfg = settings()
    headers = {"apikey": cfg.get("supabase_anon_key", ""), "Content-Type": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    headers.update(extra_headers or {})
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read())
            message = error.get("msg") or error.get("message") or error.get("error_description") or error.get("error")
        except Exception:
            message = "요청이 거부되었습니다. 설정과 계정을 확인하세요."
        raise CloudError(str(message or "클라우드 요청이 실패했습니다.")) from None
    except (OSError, TimeoutError) as exc:
        raise CloudError("클라우드 서버에 연결하지 못했습니다. 인터넷 연결을 확인하세요.") from exc


def signup(email: str, password: str) -> dict:
    cfg = settings()
    return _request(f"{cfg['supabase_url'].rstrip('/')}/auth/v1/signup", "POST",
                    {"email": email, "password": password})


def login(email: str, password: str) -> dict:
    cfg = settings()
    query = urllib.parse.urlencode({"grant_type": "password"})
    session = _request(f"{cfg['supabase_url'].rstrip('/')}/auth/v1/token?{query}", "POST",
                       {"email": email, "password": password})
    access_token = session.get("access_token")
    user = session.get("user") or {}
    if not access_token or not user.get("id"):
        raise CloudError("로그인 응답이 올바르지 않습니다.")
    check_license(access_token, user["id"])
    return {"access_token": access_token, "refresh_token": session.get("refresh_token", ""),
            "email": user.get("email", email), "user_id": user["id"]}


def check_license(access_token: str, user_id: str) -> None:
    cfg = settings()
    license_url = (f"{cfg['supabase_url'].rstrip('/')}/rest/v1/customer_licenses"
                   f"?select=status,expires_at&user_id=eq.{urllib.parse.quote(user_id)}")
    rows = _request(license_url, access_token=access_token)
    license_row = rows[0] if rows else None
    if not license_row or license_row.get("status") != "active":
        raise CloudError("계정이 등록되었지만 사용 권한이 활성화되지 않았습니다. 관리자에게 문의하세요.")
    expires = license_row.get("expires_at")
    if expires:
        try:
            expiration = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        except ValueError:
            raise CloudError("계정 사용 권한의 만료 날짜를 확인할 수 없습니다.") from None
        if expiration <= datetime.now(timezone.utc):
            raise CloudError("사용 권한이 만료되었습니다. 관리자에게 문의하세요.")


def logout(access_token: str) -> None:
    cfg = settings()
    _request(f"{cfg['supabase_url'].rstrip('/')}/auth/v1/logout", "POST", {}, access_token)


def current_access_token() -> str:
    """Refresh the child process's Supabase token before it expires."""
    global _ACCESS_TOKEN, _REFRESH_TOKEN
    try:
        part = _ACCESS_TOKEN.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        expiring = int(payload.get("exp", 0)) <= int(datetime.now(timezone.utc).timestamp()) + 300
    except Exception:
        expiring = True
    if expiring and _REFRESH_TOKEN:
        cfg = settings()
        query = urllib.parse.urlencode({"grant_type": "refresh_token"})
        session = _request(
            f"{cfg['supabase_url'].rstrip('/')}/auth/v1/token?{query}", "POST",
            {"refresh_token": _REFRESH_TOKEN},
        )
        _ACCESS_TOKEN = session.get("access_token", _ACCESS_TOKEN)
        _REFRESH_TOKEN = session.get("refresh_token", _REFRESH_TOKEN)
    if not _ACCESS_TOKEN:
        raise CloudError("클라우드 로그인 정보가 없습니다. 앱에 다시 로그인하세요.")
    return _ACCESS_TOKEN


def generate_comment(prompt: str, title: str, body: str,
                     model: str, max_body_chars: int) -> str | None:
    cfg = settings()
    payload = {"prompt": prompt, "title": title,
               "body": body[:max_body_chars], "model": model}
    result = _request(
        f"{cfg['supabase_url'].rstrip('/')}/functions/v1/generate-comment",
        "POST", payload, current_access_token(),
    )
    text = str(result.get("comment", "")).strip().strip('"').strip("'")
    if not text or "SKIP" in text.upper():
        return None
    return text.splitlines()[0][:150]


def check_update() -> dict:
    cfg = settings()
    repo = str(cfg.get("github_repository", ""))
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or "OWNER/REPOSITORY" in repo:
        return {"configured": False}
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                    "User-Agent": "AutoSNS"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            release = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"configured": True, "available": False}
        return {"configured": True, "available": False, "error": "버전을 확인하지 못했습니다."}
    except (OSError, TimeoutError, json.JSONDecodeError):
        return {"configured": True, "available": False, "error": "버전을 확인하지 못했습니다."}

    current = str(cfg.get("app_version", "0.0.0"))
    latest = str(release.get("tag_name", "")).lstrip("vV")
    version = lambda value: tuple(int(part) for part in re.findall(r"\d+", value)[:4])
    assets = release.get("assets") or []
    exe = next((item.get("browser_download_url") for item in assets
                if str(item.get("name", "")).lower().endswith(".exe")), None)
    return {"configured": True, "available": bool(latest and version(latest) > version(current)),
            "current": current, "latest": latest,
            "url": exe or release.get("html_url", "")}
