"""Public configuration and API calls for Supabase and GitHub Releases."""
from __future__ import annotations

import json
import base64
import hashlib
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from core.config import RESOURCE_DIR


class CloudError(RuntimeError):
    pass


class CloudQuotaError(CloudError):
    """The cloud-side daily comment allowance is exhausted."""
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
        if exc.code == 429:
            raise CloudQuotaError("오늘 댓글 생성 한도에 도달했어요.") from None
        raise CloudError(str(message or "클라우드 요청이 실패했습니다.")) from None
    except (OSError, TimeoutError) as exc:
        raise CloudError("클라우드 서버에 연결하지 못했습니다. 인터넷 연결을 확인하세요.") from exc


def signup(email: str, password: str) -> dict:
    cfg = settings()
    url = f"{cfg['supabase_url'].rstrip('/')}/auth/v1/signup?{urllib.parse.urlencode({'redirect_to': 'http://127.0.0.1:8765/email-verified'})}"
    return _request(url, "POST", {"email": email, "password": password})


def resend_signup_confirmation(email: str) -> dict:
    cfg = settings()
    redirect = urllib.parse.urlencode({"redirect_to": "http://127.0.0.1:8765/email-verified"})
    return _request(f"{cfg['supabase_url'].rstrip('/')}/auth/v1/resend?{redirect}", "POST",
                    {"type": "signup", "email": email})


def login(email: str, password: str) -> dict:
    cfg = settings()
    query = urllib.parse.urlencode({"grant_type": "password"})
    session = _request(f"{cfg['supabase_url'].rstrip('/')}/auth/v1/token?{query}", "POST",
                       {"email": email, "password": password})
    access_token = session.get("access_token")
    user = session.get("user") or {}
    if not access_token or not user.get("id"):
        raise CloudError("로그인 응답이 올바르지 않습니다.")
    daily_comment_limit = check_license(access_token, user["id"])
    return {"access_token": access_token, "refresh_token": session.get("refresh_token", ""),
            "email": user.get("email", email), "user_id": user["id"],
            "daily_comment_limit": daily_comment_limit}


def get_user(access_token: str) -> dict:
    """Fetch the current user record from Supabase Auth, not from cached session data."""
    cfg = settings()
    user = _request(f"{cfg['supabase_url'].rstrip('/')}/auth/v1/user", access_token=access_token)
    if not isinstance(user, dict) or not user.get("id"):
        raise CloudError("로그인 정보를 다시 확인해 주세요.")
    return user


def check_license(access_token: str, user_id: str) -> int:
    cfg = settings()
    license_url = (f"{cfg['supabase_url'].rstrip('/')}/rest/v1/customer_licenses"
                   f"?select=status,expires_at,daily_comment_limit&user_id=eq.{urllib.parse.quote(user_id)}")
    rows = _request(license_url, access_token=access_token)
    license_row = rows[0] if rows else None
    if not license_row or license_row.get("status") == "pending":
        raise CloudError("계정 정보를 불러오지 못했어요. 잠시 후 다시 로그인해 주세요.")
    if license_row.get("status") != "active":
        raise CloudError("현재 이 계정으로 로그인할 수 없어요.")
    expires = license_row.get("expires_at")
    if expires:
        try:
            expiration = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        except ValueError:
            raise CloudError("계정 정보를 확인하는 중 문제가 생겼어요. 잠시 후 다시 시도해 주세요.") from None
        if expiration <= datetime.now(timezone.utc):
            raise CloudError("이 계정의 이용 기간이 끝났어요.")
    try:
        return max(0, int(license_row.get("daily_comment_limit", 100)))
    except (TypeError, ValueError):
        raise CloudError("계정의 댓글 사용 한도를 확인하지 못했어요.") from None


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


def generate_post_topics(trend_data: dict, context_keywords: list[str],
                         access_token: str | None = None) -> dict:
    """Rank per-keyword Naver research using the hosted GPT-4o mini service."""
    cfg = settings()
    result = _request(
        f"{cfg['supabase_url'].rstrip('/')}/functions/v1/generate-post-topics",
        "POST",
        {"trend_data": trend_data, "context_keywords": context_keywords[:10]},
        access_token or current_access_token(),
    )
    recommendations = result.get("recommendations", [])
    evidence_count = result.get("search_evidence_count", 0)
    return {
        "recommendations": recommendations if isinstance(recommendations, list) else [],
        "search_grounded": bool(result.get("search_grounded", False)),
        "search_evidence_count": evidence_count if isinstance(evidence_count, int) and evidence_count > 0 else 0,
    }


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
    version_pattern = re.compile(r"^\d+(?:\.\d+){0,3}$")
    def version(value: str) -> tuple[int, int, int, int]:
        if not version_pattern.fullmatch(value):
            return (0, 0, 0, 0)
        parts = [int(part) for part in value.split(".")]
        return tuple((parts + [0, 0, 0, 0])[:4])

    if not version_pattern.fullmatch(current) or not version_pattern.fullmatch(latest):
        return {"configured": True, "available": False, "error": "버전 정보를 확인하지 못했습니다."}
    assets = release.get("assets") or []
    if not isinstance(assets, list):
        return {"configured": True, "available": False, "error": "업데이트 파일 목록을 확인하지 못했습니다."}
    installer_name = f"AutoSNS-Setup-{latest}.exe"
    installer = next((item for item in assets if item.get("name") == installer_name), None)
    manifest_asset = next((item for item in assets if item.get("name") == "AutoSNS-update.json"), None)
    if not installer or not manifest_asset:
        return {"configured": True, "available": False, "current": current, "latest": latest}
    try:
        manifest_request = urllib.request.Request(
            str(manifest_asset.get("browser_download_url", "")),
            headers={"Accept": "application/octet-stream", "User-Agent": "AutoSNS"},
        )
        with urllib.request.urlopen(manifest_request, timeout=8) as response:
            manifest = json.loads(response.read())
    except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError):
        return {"configured": True, "available": False, "error": "업데이트 정보를 확인하지 못했습니다."}
    if not isinstance(manifest, dict):
        return {"configured": True, "available": False, "error": "업데이트 정보를 확인하지 못했습니다."}

    minimum = str(manifest.get("minimum_supported_version", "0.0.0"))
    installer_url = str(manifest.get("installer_url", ""))
    digest = str(manifest.get("installer_sha256", "")).lower()
    expected_path = f"/{repo}/releases/download/{release.get('tag_name')}/{installer_name}"
    parsed_installer = urlparse(installer_url)
    if (manifest.get("version") != latest or not version_pattern.fullmatch(minimum)
            or version(minimum) > version(latest)
            or parsed_installer.scheme != "https" or parsed_installer.netloc != "github.com"
            or parsed_installer.path != expected_path or not re.fullmatch(r"[a-f0-9]{64}", digest)):
        return {"configured": True, "available": False, "error": "업데이트 파일 검증 정보가 올바르지 않습니다."}

    available = version(latest) > version(current)
    required = version(current) < version(minimum)
    return {"configured": True, "available": available, "required": required,
            "current": current, "latest": latest, "minimum_supported_version": minimum,
            "url": installer_url, "sha256": digest, "release_url": str(manifest.get("release_url", ""))}


def download_update_installer(update: dict) -> Path:
    """Download the currently advertised installer and verify its SHA-256."""
    cfg = settings()
    repo = str(cfg.get("github_repository", ""))
    latest = str(update.get("latest", ""))
    filename = f"AutoSNS-Setup-{latest}.exe"
    url = str(update.get("url", ""))
    parsed = urlparse(url)
    expected_path = f"/{repo}/releases/download/v{latest}/{filename}"
    # Git tags may omit the v prefix; check_update already validates the exact
    # tag URL. This second check keeps the downloader from accepting user input.
    if (not update.get("available") or parsed.scheme != "https" or parsed.netloc != "github.com"
            or parsed.path not in {expected_path, f"/{repo}/releases/download/{latest}/{filename}"}):
        raise CloudError("검증된 업데이트 설치 파일을 찾지 못했습니다.")
    expected_hash = str(update.get("sha256", "")).lower()
    if not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
        raise CloudError("업데이트 파일의 검증 정보가 없습니다.")

    request = urllib.request.Request(url, headers={"User-Agent": "AutoSNS"})
    digest = hashlib.sha256()
    destination = Path(tempfile.gettempdir()) / filename
    descriptor, temporary_name = tempfile.mkstemp(prefix="autosns-update-", suffix=".download")
    os.close(descriptor)
    temp_path = Path(temporary_name)
    try:
        with urllib.request.urlopen(request, timeout=120) as response, temp_path.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
                if output.tell() > 600 * 1024 * 1024:
                    raise CloudError("업데이트 설치 파일 크기가 허용 범위를 넘었습니다.")
        if digest.hexdigest() != expected_hash:
            raise CloudError("업데이트 설치 파일 검증에 실패했습니다. 다시 시도해 주세요.")
        os.replace(temp_path, destination)
        return destination
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise CloudError("업데이트 설치 파일을 다운로드하지 못했습니다.") from exc
    except CloudError:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
