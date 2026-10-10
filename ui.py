"""AutoSNS dashboard launcher. Run with: python main.py --ui"""
from __future__ import annotations
import json
import hashlib
import hmac
import asyncio
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
from collections import deque
from urllib.parse import parse_qs, urlparse
import webbrowser
from datetime import date, datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core.config import BASE_DIR, CONFIG_FILE, RESOURCE_DIR, load_config, save_config
from core import cloud
from core.credentials import CredentialVaultUnavailable, load_naver_credentials, save_naver_credentials
from core.llm import resolve_api_key
from core.storage import ACTIVITY_FILE, TARGETS_FILE, DAILY_FILE, account_data_dir, data_file, load_json, save_json
from features.creator_trends import recommend_related_topics, recommend_topics

ROOT = RESOURCE_DIR
PAGE = ROOT / "dashboard.html"
AUTH_USERS_FILE = BASE_DIR / "auth_users.json"
AUTH_ITERATIONS = 160_000
AUTH_LOCK = threading.Lock()
AUTH_SESSIONS: dict[str, dict] = {}
AUTH_RECHECK_SECONDS = 5

FEATURE_LABELS = {"buddy": "서이추", "comment": "댓글", "like": "공감", "publish": "포스팅 업로드"}
DEFAULT_POST_SETTINGS = {
    "keywords": [], "writing_mode": "ai", "style": "auto", "length": "auto",
    "images_enabled": True, "image_count": 0, "image_count_auto": True, "image_source": "ai",
    "use_cover_image": True, "use_thumbnail": True, "add_hashtags": True, "add_subheading": True,
    "add_links": False, "add_cta": False, "publish_mode": "now", "scheduled_at": "",
    "timezone": "Asia/Seoul", "use_local_timezone": True,
}
RUN_LOCK = threading.Lock()
RUN_PROCESS: subprocess.Popen | None = None
RUN_MODE: str | None = None
RUN_ACCOUNT_ID: str | None = None
RUN_STOP_REQUESTED = False
PUBLISH_LOCK = threading.Lock()
PUBLISHING_POSTS: set[tuple[str, str]] = set()
UI_API_VERSION = 28
RUN_LOG_LOCK = threading.Lock()
RUN_LOGS: deque[dict] = deque(maxlen=500)
RUN_LOG_ID = 0


def _append_run_log(message: str) -> None:
    global RUN_LOG_ID
    message = message.strip()
    if not message:
        return
    with RUN_LOG_LOCK:
        RUN_LOG_ID += 1
        RUN_LOGS.append({"id": RUN_LOG_ID,
                         "time": datetime.now().astimezone().strftime("%H:%M:%S"),
                         "message": message})


def _capture_run_output(process: subprocess.Popen) -> None:
    stream = process.stdout
    if stream is None:
        return
    try:
        for line in iter(stream.readline, ""):
            _append_run_log(line)
    finally:
        try:
            stream.close()
        except Exception:
            pass
def _accounts_file(handler) -> Path:
    session = _auth_session(handler) or {}
    owner = hashlib.sha256(str(session.get("email", "local")).encode("utf-8")).hexdigest()[:24]
    folder = BASE_DIR / "sns_account_lists"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{owner}.json"

def _load_accounts(handler) -> list[dict]:
    rows = load_json(_accounts_file(handler), [])
    return rows if isinstance(rows, list) else []

def _save_accounts(handler, rows: list[dict]) -> None:
    _accounts_file(handler).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def _valid_email_address(value: str) -> bool:
    if len(value) > 254 or value.count("@") != 1 or any(ch.isspace() for ch in value):
        return False
    local, domain = value.rsplit("@", 1)
    if (not local or len(local) > 64 or local.startswith(".") or local.endswith(".")
            or ".." in local
            or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)):
        return False
    try:
        ascii_domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    labels = ascii_domain.split(".")
    return (len(labels) >= 2 and all(
        1 <= len(label) <= 63 and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
        for label in labels
    ) and len(labels[-1]) >= 2 and (labels[-1].isalpha() or labels[-1].startswith("xn--")))


def _password_error(value: str) -> str | None:
    if (len(value) < 8 or not re.search(r"[A-Za-z]", value)
            or not re.search(r"[0-9]", value)):
        return "비밀번호는 8자 이상이며 영문자와 숫자를 각각 포함해야 합니다."
    return None


def _friendly_auth_error(message: str) -> str:
    lowered = message.lower()
    if any(term in lowered for term in (
        "customer_licenses", "schema cache", "pgrst", "permission denied",
        "계정 사용 설정", "relation does not exist",
    )):
        return "계정 정보를 불러오지 못했어요. 잠시 후 다시 시도해 주세요."
    if "already registered" in lowered or "already exists" in lowered:
        return "이미 가입한 이메일이에요. 로그인해 주세요."
    if "email not confirmed" in lowered or "email_not_confirmed" in lowered:
        return "메일 인증을 마친 뒤 로그인해 주세요. 메일이 보이지 않으면 스팸함을 확인해 주세요."
    if "invalid login credentials" in lowered:
        return "이메일 또는 비밀번호를 확인해 주세요."
    if "invalid email" in lowered:
        return "이메일 주소 형식을 확인해 주세요."
    if "password" in lowered and ("weak" in lowered or "short" in lowered or "characters" in lowered):
        return "비밀번호는 8자 이상이며 영문자와 숫자를 각각 포함해야 합니다."
    if "rate limit" in lowered or "security purposes" in lowered:
        return "인증 메일을 너무 자주 요청했어요. 잠시 기다렸다 다시 시도해 주세요."
    return "요청을 처리하지 못했어요. 잠시 후 다시 시도해 주세요."


def _password_hash(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, AUTH_ITERATIONS).hex()


def _load_auth_users() -> dict:
    try:
        users = json.loads(AUTH_USERS_FILE.read_text(encoding="utf-8"))
        if isinstance(users, dict):
            return users
    except (OSError, json.JSONDecodeError):
        pass
    salt = b"autosns-test-account-salt"
    return {"test": {"salt": salt.hex(), "password_hash": _password_hash("test", salt)}}


def _save_auth_users(users: dict) -> None:
    AUTH_USERS_FILE.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")


def _session_from_request(handler) -> str | None:
    cookies = SimpleCookie()
    try:
        cookies.load(handler.headers.get("Cookie", ""))
        return cookies["autosns_session"].value if "autosns_session" in cookies else None
    except Exception:
        return None


def _is_authenticated(handler) -> bool:
    return _auth_session(handler) is not None


def _auth_session(handler, force_recheck: bool = False) -> dict | None:
    token = _session_from_request(handler)
    with AUTH_LOCK:
        session = AUTH_SESSIONS.get(token) if token else None
    if not session:
        return None
    if not cloud.supabase_configured():
        return session

    now = time.monotonic()
    checked_at = float(session.get("_license_checked_at", 0))
    if force_recheck or now - checked_at >= AUTH_RECHECK_SECONDS:
        try:
            access_token = session.get("access_token", "")
            user = cloud.get_user(access_token)
            if user.get("id") != session.get("user_id"):
                raise cloud.CloudError("Supabase user no longer matches this session")
            session["daily_comment_limit"] = cloud.check_license(access_token, session.get("user_id", ""))
        except cloud.CloudError:
            with AUTH_LOCK:
                if token in AUTH_SESSIONS:
                    AUTH_SESSIONS.pop(token, None)
            return None
        with AUTH_LOCK:
            current = AUTH_SESSIONS.get(token)
            if current is not session:
                return None
            session["_license_checked_at"] = now
    return session


def _activity_rows(account_id: str | None = None) -> list[dict]:
    rows = load_json(data_file("activity_log.json", account_id), [])
    if not isinstance(rows, list):
        rows = []

    # 기존 targets.json에는 각 대상의 마지막 처리 결과가 남아 있으므로
    # 새 활동 로그가 만들어지기 전에도 실제 완료 기록은 표시할 수 있습니다.
    seen = {
        (r.get("blog_id"), r.get("feature"), r.get("status"),
         str(r.get("at", ""))[:10], r.get("detail", ""))
        for r in rows if isinstance(r, dict)
    }
    targets = load_json(data_file("targets.json", account_id), {})
    for blog_id, info in targets.items():
        if not isinstance(info, dict):
            continue
        for feature in FEATURE_LABELS:
            result = info.get(feature)
            if not isinstance(result, dict) or not result.get("status"):
                continue
            day = str(result.get("date", ""))
            signature = (blog_id, feature, result["status"], day, result.get("detail", ""))
            if signature not in seen:
                rows.append({"at": day, "blog_id": blog_id, "feature": feature,
                             "status": result["status"], "detail": result.get("detail", "")})
                seen.add(signature)
    rows.sort(key=lambda row: str(row.get("at", "")), reverse=True)
    return rows


def blog_state(account_id: str | None = None) -> dict:
    cfg = load_config()
    daily = load_json(data_file("daily_count.json", account_id), {})
    counts = daily.get(str(date.today()), {}) if isinstance(daily, dict) else {}
    if isinstance(counts, int):
        counts = {"buddy": counts}
    if not isinstance(counts, dict):
        counts = {}

    limit_mode = cfg.get("general", {}).get("daily_limit_mode", "shared")
    limits = {feature: int(cfg.get(feature, {}).get("daily_limit", 100))
              for feature in ("buddy", "comment", "like")}
    total_limit = (sum(limits.values()) if limit_mode == "individual"
                   else int(cfg.get("general", {}).get("daily_task_limit", 100)))
    labels = {"buddy": "서이추", "comment": "댓글", "like": "공감"}
    tasks = [
        {"feature": feature, "label": labels[feature],
         "count": int(counts.get(feature, 0)),
         **({"limit": limits[feature]} if limit_mode == "individual" else {})}
        for feature in ("buddy", "comment", "like")
    ]
    total = sum(item["count"] for item in tasks)

    rows = _activity_rows(account_id)
    templates = cfg.get("buddy", {}).get("messages", [])
    if not isinstance(templates, list):
        templates = []
    template_rows = []
    for message in templates:
        uses = [r for r in rows if r.get("feature") == "buddy"
                and r.get("status") == "done" and r.get("detail") == message]
        template_rows.append({
            "message": message,
            "used": len(uses),
            "last_used": next((str(r.get("at", ""))[:10] for r in uses), ""),
        })

    activity = []
    for row in rows[:8]:
        feature = row.get("feature", "")
        blog_id = str(row.get("blog_id", ""))
        status = row.get("status", "")
        when = str(row.get("at", ""))
        try:
            stamp = datetime.fromisoformat(when)
            time_label = stamp.astimezone().strftime("%H:%M") if "T" in when else stamp.strftime("%m.%d")
        except ValueError:
            time_label = when[5:10].replace("-", ".") if len(when) >= 10 else ""
        action = {
            "buddy": f"{blog_id} 님에게 이웃 추가",
            "comment": f"{blog_id} 님 게시글에 댓글 작성",
            "like": f"{blog_id} 님 게시글에 공감",
        }.get(feature, row.get("detail", ""))
        activity.append({
            "time": time_label,
            "feature": FEATURE_LABELS.get(feature, feature),
            "action": action,
            "status": status,
        })

    post_settings = load_json(data_file("blog_post_settings.json", account_id), {})
    post_settings = {**DEFAULT_POST_SETTINGS, **post_settings} if isinstance(post_settings, dict) else dict(DEFAULT_POST_SETTINGS)
    return {
        "activity": activity,
        "activity_total": len(rows),
        "tasks": tasks,
        "limit_mode": limit_mode,
        "total": total,
        "total_limit": total_limit,
        "progress": round(total * 100 / total_limit) if total_limit else 0,
        "templates": template_rows,
        "features": {
            feature: bool(cfg.get(feature, {}).get("enabled", False))
            for feature in FEATURE_LABELS
        },
        "posts": load_json(data_file("blog_posts.json", account_id), []),
        "post_settings": post_settings,
        "keywords": [],
    }

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def _local_request_allowed(self, require_origin: bool = False) -> bool:
        # Reject DNS-rebinding/host-header access and browser requests initiated
        # by pages other than the AutoSNS UI. The app intentionally serves only
        # this fixed loopback origin.
        if self.headers.get("Host", "").lower() != "127.0.0.1:8765":
            return False
        origin = self.headers.get("Origin")
        if require_origin and origin != "http://127.0.0.1:8765":
            return False
        return require_origin is False or origin == "http://127.0.0.1:8765"
    def _json(self, obj, code=200, headers=None):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
        for key, value in (headers or {}).items(): self.send_header(key, value)
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def _body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
    def _query_account_id(self) -> str | None:
        account_id = parse_qs(urlparse(self.path).query).get("account_id", [""])[0]
        if not re.fullmatch(r"[0-9a-f]{32}", account_id):
            return None
        return account_id if any(row.get("id") == account_id for row in _load_accounts(self)) else None
    def do_GET(self):
        global RUN_MODE
        if not self._local_request_allowed():
            self._json({"error": "허용되지 않은 로컬 요청입니다."}, 403); return
        route = urlparse(self.path).path
        if route in {"/autosns-mark.svg", "/favicon.svg"}:
            data = (ROOT / "autosns-mark.svg").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if route.startswith("/assets/sidebar-icons/"):
            filename = route.rsplit("/", 1)[-1]
            if filename not in {"dashboard.png", "blog.png", "blog.svg", "instagram.png", "youtube.png", "threads.png", "settings.png"}:
                self.send_error(404); return
            data = (ROOT / "assets" / "sidebar-icons" / filename).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml; charset=utf-8" if filename.endswith(".svg") else "image/png")
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if route == "/api/session":
            session = _auth_session(self, force_recheck=True)
            headers = {}
            if _session_from_request(self) and session is None:
                headers["Set-Cookie"] = "autosns_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
            self._json({"authenticated": session is not None,
                        "username": session.get("email") if session else None,
                        "daily_comment_limit": session.get("daily_comment_limit") if session else None,
                        "cloud_auth": cloud.supabase_configured(),
                        "api_version": UI_API_VERSION}, headers=headers); return
        if self.path.startswith("/api/") and not _is_authenticated(self):
            self._json({"error": "로그인이 필요합니다."}, 401, headers={
                "Set-Cookie": "autosns_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
            }); return
        if route == "/api/accounts":
            rows = []
            for account in _load_accounts(self):
                cookie_file = account_data_dir(account.get("id")) / "naver_cookies.json"
                rows.append({**account, "connected": cookie_file.exists()})
            self._json({"accounts": rows}); return
        if route == "/api/state":
            cfg = load_config(); cfg.get("comment", {})["api_key"] = ""
            account_id = self._query_account_id()
            targets = load_json(data_file("targets.json", account_id), {}) if account_id else {}
            daily = load_json(data_file("daily_count.json", account_id), {}) if account_id else {}
            self._json({"config": cfg, "targets": targets, "daily": daily}); return
        if route == "/api/blog":
            account_id = self._query_account_id()
            self._json(blog_state(account_id) if account_id else {"connected": False}); return
        if route == "/api/posts/image":
            account_id = self._query_account_id()
            query = parse_qs(urlparse(self.path).query)
            post_id = query.get("post_id", [""])[0]
            image_id = query.get("image_id", [""])[0]
            if not account_id or not re.fullmatch(r"[0-9a-f]{32}", post_id) or not re.fullmatch(r"image-[1-5]", image_id):
                self.send_error(404); return
            posts = load_json(data_file("blog_posts.json", account_id), [])
            post = next((row for row in posts if row.get("id") == post_id), None) if isinstance(posts, list) else None
            image = next((row for row in (post or {}).get("images", []) if row.get("id") == image_id), None)
            if not image:
                self.send_error(404); return
            try:
                image_path = Path(image.get("path", "")).resolve()
                image_path.relative_to((account_data_dir(account_id) / "blog_post_images" / post_id).resolve())
                data = image_path.read_bytes()
            except (OSError, ValueError):
                self.send_error(404); return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if route == "/api/run-status":
            with RUN_LOCK:
                process = RUN_PROCESS
                running = process is not None and process.poll() is None
                self._json({"running": running,
                            "mode": RUN_MODE,
                            "account_id": RUN_ACCOUNT_ID,
                            "stopped": RUN_STOP_REQUESTED and not running,
                            "exit_code": None if running or process is None else process.returncode})
            return
        if route == "/api/run-logs":
            try:
                after = max(0, int(parse_qs(urlparse(self.path).query).get("after", ["0"])[0]))
            except (TypeError, ValueError):
                after = 0
            with RUN_LOG_LOCK:
                logs = [row for row in RUN_LOGS if row["id"] > after]
                last_id = RUN_LOG_ID
            self._json({"logs": logs, "last_id": last_id})
            return
        if route == "/api/update-check":
            self._json(cloud.check_update()); return
        if route == "/api/app-version":
            self._json({"current": str(cloud.settings().get("app_version", "0.0.0"))}); return
        data = PAGE.read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_POST(self):
        global RUN_PROCESS, RUN_MODE, RUN_ACCOUNT_ID, RUN_STOP_REQUESTED, RUN_LOG_ID
        if not self._local_request_allowed(require_origin=True):
            self._json({"error": "허용되지 않은 로컬 요청입니다."}, 403); return
        if self.path == "/api/apply-update":
            try:
                if sys.platform != "win32" or not getattr(sys, "frozen", False):
                    self._json({"error": "설치 파일 자동 실행은 배포된 Windows 앱에서 사용할 수 있어요."}, 400); return
                body = self._body()
                update = cloud.check_update()
                requested_version = str(body.get("version", ""))
                if not update.get("available") or requested_version != update.get("latest"):
                    self._json({"error": "최신 업데이트 정보를 다시 확인한 뒤 시도해 주세요."}, 409); return
                with RUN_LOCK:
                    if RUN_PROCESS is not None and RUN_PROCESS.poll() is None:
                        self._json({"error": "진행 중인 자동화 작업을 마친 뒤 업데이트해 주세요."}, 409); return
                with PUBLISH_LOCK:
                    if PUBLISHING_POSTS:
                        self._json({"error": "네이버 포스팅 작업을 마친 뒤 업데이트해 주세요."}, 409); return
                installer = cloud.download_update_installer(update)
                installer_log = installer.with_suffix(".install.log")
                subprocess.Popen(
                    [str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-",
                     f"/LOG={installer_log}"],
                    cwd=str(installer.parent),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                   | getattr(subprocess, "DETACHED_PROCESS", 0)),
                )
                self._json({"ok": True, "installing": True})
                # Release the executable before Inno Setup replaces it and
                # starts the newly installed copy. The installer remains open
                # if the user cancels; they can reopen the current version.
                threading.Timer(1.5, os._exit, args=(0,)).start()
            except Exception as exc:
                self._json({"error": str(exc) or "업데이트 설치를 시작하지 못했어요."}, 502)
            return
        if self.path in {"/api/auth/login", "/api/auth/signup", "/api/auth/resend"}:
            try:
                body = self._body()
                username = str(body.get("email", body.get("username", ""))).strip().lower()
                password = str(body.get("password", ""))
                if not _valid_email_address(username):
                    self._json({"error": "올바른 이메일 주소를 입력해 주세요."}, 400); return
                if self.path == "/api/auth/resend":
                    if not cloud.supabase_configured():
                        self._json({"error": "이메일 인증을 사용할 수 없어요. 잠시 후 다시 시도해 주세요."}, 400); return
                    cloud.resend_signup_confirmation(username)
                    self._json({"ok": True, "message": "인증 메일을 다시 보냈어요. 받은편지함을 확인해 주세요."})
                    return
                if not password:
                    self._json({"error": "비밀번호를 입력해 주세요."}, 400); return
                if cloud.supabase_configured():
                    if self.path == "/api/auth/signup":
                        if password != str(body.get("password_confirmation", "")):
                            self._json({"error": "비밀번호가 서로 일치하지 않습니다."}, 400); return
                        password_error = _password_error(password)
                        if password_error:
                            self._json({"error": password_error}, 400); return
                        signup_result = cloud.signup(username, password)
                        # GoTrue's raw REST endpoint returns access/refresh tokens
                        # at the top level when email auto-confirm is enabled.
                        verification_required = not bool(
                            signup_result.get("access_token") and signup_result.get("refresh_token")
                        )
                        message = "" if verification_required else "인증 메일을 보내지 못했어요. 잠시 후 다시 시도해 주세요."
                        self._json({"ok": True, "signup_pending": True,
                                    "verification_required": verification_required,
                                    "message": message}, 201)
                        return
                    account = cloud.login(username, password)
                    token = secrets.token_urlsafe(32)
                    with AUTH_LOCK:
                        AUTH_SESSIONS[token] = account
                    self._json({"ok": True, "username": account["email"],
                                "daily_comment_limit": account.get("daily_comment_limit")}, headers={
                        "Set-Cookie": f"autosns_session={token}; Path=/; HttpOnly; SameSite=Lax"
                    })
                    return
                with AUTH_LOCK:
                    users = _load_auth_users()
                    if self.path == "/api/auth/signup":
                        if password != str(body.get("password_confirmation", "")):
                            self._json({"error": "비밀번호가 서로 일치하지 않습니다."}, 400); return
                        password_error = _password_error(password)
                        if password_error:
                            self._json({"error": password_error}, 400); return
                        if username in users:
                            self._json({"error": "이미 사용 중인 아이디입니다."}, 409); return
                        salt = secrets.token_bytes(16)
                        users[username] = {"salt": salt.hex(), "password_hash": _password_hash(password, salt)}
                        _save_auth_users(users)
                    record = users.get(username)
                    valid = False
                    if record:
                        salt = bytes.fromhex(record["salt"])
                        valid = hmac.compare_digest(record["password_hash"], _password_hash(password, salt))
                    if not valid:
                        self._json({"error": "아이디 또는 비밀번호를 확인하세요."}, 401); return
                    token = secrets.token_urlsafe(32)
                    AUTH_SESSIONS[token] = {"email": username}
                self._json({"ok": True, "username": username}, headers={
                    "Set-Cookie": f"autosns_session={token}; Path=/; HttpOnly; SameSite=Lax"
                })
            except Exception as e:
                self._json({"error": _friendly_auth_error(str(e))}, 400)
            return
        if self.path == "/api/auth/logout":
            token = _session_from_request(self)
            with AUTH_LOCK:
                session = AUTH_SESSIONS.pop(token, None) if token else None
            if session and cloud.supabase_configured():
                try:
                    cloud.logout(session.get("access_token", ""))
                except cloud.CloudError:
                    pass
            self._json({"ok": True}, headers={
                "Set-Cookie": "autosns_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
            })
            return
        if self.path.startswith("/api/") and not _is_authenticated(self):
            self._json({"error": "로그인이 필요합니다."}, 401, headers={
                "Set-Cookie": "autosns_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
            }); return
        if self.path == "/api/accounts":
            try:
                body = self._body()
                name = str(body.get("name", "")).strip()[:60]
                if not name:
                    self._json({"error": "계정 이름을 입력해 주세요."}, 400); return
                account = {"id": uuid.uuid4().hex, "name": name, "platform": "blog", "created_at": datetime.now().astimezone().isoformat(timespec="seconds")}
                rows = _load_accounts(self); rows.append(account); _save_accounts(self, rows)
                self._json({"account": account}, 201)
            except Exception:
                self._json({"error": "계정을 추가하지 못했어요."}, 500)
            return
        if self.path in {"/api/run", "/api/login"}:
            login_only = self.path == "/api/login"
            try:
                body = self._body()
            except Exception:
                body = {}
            account_id = str(body.get("account_id", ""))
            credentials = body.get("credentials") if login_only else None
            if credentials is not None:
                if not isinstance(credentials, dict):
                    self._json({"error": "네이버 로그인 정보를 확인해 주세요."}, 400); return
                username = str(credentials.get("username", "")).strip()
                password = str(credentials.get("password", ""))
                if not username or not password or len(username) > 128 or len(password) > 256:
                    self._json({"error": "네이버 아이디와 비밀번호를 확인해 주세요."}, 400); return
                credentials = {"username": username, "password": password}
                username = ""
                password = ""
                if isinstance(body, dict):
                    body["credentials"] = None
            account = next((row for row in _load_accounts(self) if row.get("id") == account_id), None)
            if not account:
                if credentials is not None:
                    credentials["username"] = ""; credentials["password"] = ""
                self._json({"error": "먼저 연결할 계정을 추가하고 선택해 주세요."}, 400); return
            if login_only and credentials is not None:
                try:
                    save_naver_credentials(account_id, credentials["username"], credentials["password"])
                except CredentialVaultUnavailable as e:
                    credentials["username"] = ""; credentials["password"] = ""
                    self._json({"error": "이 PC의 계정별 로그인 정보 파일에 저장하지 못했어요. 파일 권한과 저장 공간을 확인해 주세요."}, 503); return
                except Exception:
                    credentials["username"] = ""; credentials["password"] = ""
                    self._json({"error": "이 PC의 계정별 로그인 정보 파일에 저장하지 못했어요. 파일 권한과 저장 공간을 확인해 주세요."}, 503); return
            elif login_only:
                try:
                    credentials = load_naver_credentials(account_id)
                except CredentialVaultUnavailable:
                    credentials = None
                    if body.get("require_saved_credentials"):
                        self._json({"error": "저장된 네이버 로그인 정보를 읽을 수 없어요. 계정 설정에서 아이디와 비밀번호를 다시 저장해 주세요."}, 503)
                        return
                except Exception:
                    credentials = None
                    if body.get("require_saved_credentials"):
                        self._json({"error": "네이버 로그인 정보 파일을 읽지 못했어요. 파일 권한을 확인한 뒤 계정 설정에서 다시 저장해 주세요."}, 500)
                        return
                if body.get("require_saved_credentials") and credentials is None:
                    self._json({"error": "이 계정의 저장된 네이버 아이디·비밀번호를 찾지 못했어요. 계정 설정에서 로그인 정보를 다시 저장해 주세요."}, 409)
                    return
            cookie_file = account_data_dir(account_id) / "naver_cookies.json"
            if not login_only and not cookie_file.exists():
                self._json({"error": "선택한 계정에 먼저 로그인해 주세요."}, 409); return
            session = _auth_session(self)
            if not login_only and cloud.supabase_configured() and session:
                try:
                    cloud.check_license(session["access_token"], session["user_id"])
                except cloud.CloudError as e:
                    self._json({"error": str(e)}, 403)
                    return
            with RUN_LOCK:
                if RUN_PROCESS is not None and RUN_PROCESS.poll() is None:
                    if credentials is not None:
                        credentials["username"] = ""; credentials["password"] = ""
                    self._json({"running": True, "message": "다른 작업이 이미 실행 중입니다."}, 409)
                    return
                try:
                    if getattr(sys, "frozen", False):
                        command = [sys.executable, "--login-only" if login_only else "--automation-child"]
                    else:
                        command = [sys.executable, str(ROOT / "main.py")]
                        if login_only:
                            command.append("--login-only")
                    if login_only and credentials is not None:
                        command.append("--login-only-credentials")
                    env = os.environ.copy()
                    env["AUTOSNS_ACCOUNT_ID"] = account_id
                    env["PYTHONUNBUFFERED"] = "1"
                    env["PYTHONIOENCODING"] = "utf-8"
                    if session and session.get("access_token"):
                        env["AUTOSNS_SUPABASE_ACCESS_TOKEN"] = session["access_token"]
                        env["AUTOSNS_SUPABASE_REFRESH_TOKEN"] = session.get("refresh_token", "")
                    RUN_PROCESS = subprocess.Popen(
                        command,
                        cwd=str(BASE_DIR),
                        env=env,
                        stdin=subprocess.PIPE if login_only and credentials is not None else subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                        text=True,
                        start_new_session=(sys.platform != "win32"),
                    )
                    if login_only and credentials is not None:
                        try:
                            RUN_PROCESS.stdin.write(json.dumps(credentials, ensure_ascii=False) + "\n")
                            RUN_PROCESS.stdin.flush()
                            RUN_PROCESS.stdin.close()
                        except Exception:
                            RUN_PROCESS.terminate()
                            RUN_PROCESS = None
                            self._json({"error": "로그인 정보를 전달하지 못했어요. 다시 시도해 주세요."}, 500)
                            return
                        finally:
                            credentials["username"] = ""
                            credentials["password"] = ""
                    RUN_MODE = "login" if login_only else "work"
                    RUN_ACCOUNT_ID = account_id
                    RUN_STOP_REQUESTED = False
                    with RUN_LOG_LOCK:
                        RUN_LOGS.clear()
                        RUN_LOG_ID = 0
                    if not login_only:
                        _append_run_log("작업을 시작했어요.")
                    threading.Thread(target=_capture_run_output, args=(RUN_PROCESS,), daemon=True).start()
                except Exception as e:
                    if credentials is not None:
                        credentials["username"] = ""; credentials["password"] = ""
                    self._json({"error": str(e)}, 500)
                    return
            message = ("저장된 네이버 아이디로 자동 로그인을 시도합니다. 본인 확인이 나오면 브라우저에서 완료해 주세요."
                       if login_only and credentials is not None else
                       "네이버 로그인 창이 열렸어요. 로그인한 뒤 브라우저 창을 닫아 주세요."
                       if login_only else "작업을 시작했습니다.")
            self._json({"running": True, "message": message})
            return
        if self.path == "/api/stop":
            with RUN_LOCK:
                process = RUN_PROCESS
                if process is None or process.poll() is not None or RUN_MODE != "work":
                    self._json({"stopped": False, "message": "실행 중인 작업이 없습니다."}, 409)
                    return
                try:
                    if sys.platform == "win32":
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            check=False, timeout=10,
                        )
                    else:
                        os.killpg(process.pid, signal.SIGTERM)
                    RUN_STOP_REQUESTED = True
                    _append_run_log("작업 중지를 요청했어요.")
                except (OSError, subprocess.SubprocessError):
                    try:
                        process.terminate()
                        RUN_STOP_REQUESTED = True
                    except OSError:
                        self._json({"stopped": False, "message": "작업을 중지하지 못했어요."}, 500)
                        return
            self._json({"stopped": True, "message": "작업을 중지했어요."})
            return
        if self.path == "/api/feature":
            try:
                body = self._body()
                account_id = str(body.get("account_id", ""))
                account = next((row for row in _load_accounts(self) if row.get("id") == account_id), None)
                if not account or not (account_data_dir(account_id) / "naver_cookies.json").exists():
                    self._json({"error": "연결된 계정을 선택해 주세요."}, 409)
                    return
                feature = body.get("feature")
                if feature not in FEATURE_LABELS or not isinstance(body.get("enabled"), bool):
                    self._json({"error": "지원하지 않는 기능 설정입니다."}, 400)
                    return
                cfg = load_config()
                if feature == "comment" and body["enabled"] and not resolve_api_key(cfg["comment"]):
                    self._json({"error": "자동 댓글을 켜려면 config.json에 OpenAI API 키를 설정하세요."}, 400)
                    return
                cfg[feature]["enabled"] = body["enabled"]
                save_config(cfg)
                self._json({"ok": True, "feature": feature, "enabled": body["enabled"]})
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return
        if self.path == "/api/posts/recommend-keywords":
            try:
                body = self._body()
                account_id = str(body.get("account_id", ""))
                if not re.fullmatch(r"[0-9a-f]{32}", account_id) or not any(
                    row.get("id") == account_id for row in _load_accounts(self)
                ):
                    self._json({"error": "블로그 계정을 선택해 주세요."}, 400); return
                cfg = load_config()
                api_key = str(cfg.get("comment", {}).get("api_key", "") or os.environ.get("OPENAI_API_KEY", "")).strip()
                session = _auth_session(self)
                use_cloud = cloud.supabase_configured()
                if use_cloud and not str((session or {}).get("access_token", "")):
                    self._json({"error": "클라우드 로그인 정보가 없습니다. 앱에서 다시 로그인해 주세요."}, 401); return
                result = asyncio.run(recommend_topics(
                    account_id, [], api_key,
                    use_cloud=use_cloud,
                    access_token=str((session or {}).get("access_token", "")) or None,
                ))
                self._json(result)
            except Exception as e:
                self._json({"error": str(e) or "트렌드 키워드 추천을 받지 못했어요."}, 502)
            return
        if self.path == "/api/posts/recommend-related-topics":
            try:
                body = self._body()
                keyword = str(body.get("keyword", "")).strip()[:40]
                if not keyword:
                    self._json({"error": "연관 주제를 찾을 키워드를 입력해 주세요."}, 400); return
                cfg = load_config()
                api_key = str(cfg.get("comment", {}).get("api_key", "") or os.environ.get("OPENAI_API_KEY", "")).strip()
                if not cloud.supabase_configured():
                    raise RuntimeError("네이버 검색 기반 주제 추천을 사용하려면 앱의 Supabase 연결을 설정해 주세요.")
                session = _auth_session(self)
                access_token = str((session or {}).get("access_token", ""))
                if not access_token:
                    raise RuntimeError("클라우드 로그인 정보가 없습니다. 앱에서 로그아웃한 뒤 다시 로그인해 주세요.")
                result = asyncio.run(recommend_related_topics(
                    keyword, api_key,
                    # Related topics must use current NAVER Search evidence,
                    # even when this installation has a local OpenAI key.
                    use_cloud=True,
                    access_token=access_token,
                ))
                self._json(result)
            except Exception as e:
                self._json({"error": str(e) or "연관 주제 추천을 받지 못했어요."}, 502)
            return
        if self.path == "/api/posts/generate":
            try:
                body = self._body()
                account_id = str(body.get("account_id", ""))
                if not re.fullmatch(r"[0-9a-f]{32}", account_id) or not any(row.get("id") == account_id for row in _load_accounts(self)):
                    self._json({"error": "블로그 계정을 선택해 주세요."}, 400); return
                settings = load_json(data_file("blog_post_settings.json", account_id), {})
                if not isinstance(settings, dict): settings = {}
                settings = {**DEFAULT_POST_SETTINGS, **settings}
                requested_keywords = body.get("keywords")
                if requested_keywords is not None:
                    if not isinstance(requested_keywords, list) or len(requested_keywords) != 1:
                        self._json({"error": "포스팅 생성 요청에는 키워드 하나만 지정해 주세요."}, 400); return
                    keyword = str(requested_keywords[0]).strip()[:40]
                    if not keyword:
                        self._json({"error": "포스팅 키워드를 확인해 주세요."}, 400); return
                    settings["keywords"] = [keyword]
                elif isinstance(settings.get("keywords"), list) and len(settings["keywords"]) > 1:
                    self._json({"error": "여러 키워드는 키워드별로 나누어 생성해야 해요."}, 400); return
                cfg = load_config()
                api_key = str(cfg.get("comment", {}).get("api_key", "") or os.environ.get("OPENAI_API_KEY", "")).strip()
                use_cloud = cloud.supabase_configured()
                session = _auth_session(self) if use_cloud else None
                access_token = str((session or {}).get("access_token", ""))
                if use_cloud and not access_token:
                    self._json({"error": "클라우드 로그인 정보가 없습니다. 앱에서 다시 로그인해 주세요."}, 401); return
                if not use_cloud and not api_key:
                    self._json({"error": "포스팅 생성에 사용할 OpenAI API 키를 설정해 주세요."}, 400); return
                from core.blog_writer import generate_blog_draft
                post = generate_blog_draft(api_key, settings, account_id, access_token=access_token or None)
                posts_path = data_file("blog_posts.json", account_id)
                posts = load_json(posts_path, [])
                if not isinstance(posts, list): posts = []
                now = datetime.now().astimezone().isoformat(timespec="seconds")
                post.update({"created_at": now, "updated_at": now})
                posts.insert(0, post)
                save_json(posts_path, posts)
                self._json({"ok": True, "post": post, "posts": posts})
            except Exception as e:
                self._json({"error": str(e) or "포스팅을 생성하지 못했어요."}, 502)
            return
        if self.path == "/api/posts/publish":
            try:
                body = self._body()
                account_id = str(body.get("account_id", ""))
                post_id = str(body.get("post_id", ""))
                if not re.fullmatch(r"[0-9a-f]{32}", account_id) or not any(row.get("id") == account_id for row in _load_accounts(self)):
                    self._json({"error": "블로그 계정을 선택해 주세요."}, 400); return
                if not re.fullmatch(r"[0-9a-f]{32}", post_id):
                    self._json({"error": "발행할 포스팅을 확인해 주세요."}, 400); return
                posts_path = data_file("blog_posts.json", account_id)
                posts = load_json(posts_path, [])
                post = next((row for row in posts if row.get("id") == post_id), None) if isinstance(posts, list) else None
                if not post:
                    self._json({"error": "발행할 포스팅을 찾을 수 없습니다."}, 404); return
                if post.get("status") == "published":
                    self._json({"error": "이미 발행된 포스팅입니다.", "url": post.get("url", "")}, 409); return
                publish_key = (account_id, post_id)
                with PUBLISH_LOCK:
                    if publish_key in PUBLISHING_POSTS:
                        self._json({"error": "이미 발행 작업을 진행하고 있어요. 완료 응답을 기다려 주세요."}, 409); return
                    PUBLISHING_POSTS.add(publish_key)
                try:
                    from features.blog_publisher import publish_blog_post
                    result = publish_blog_post(post, account_id, session_prepared=bool(body.get("session_prepared")))
                    now = datetime.now().astimezone().isoformat(timespec="seconds")
                    post.update({"status": "published", "published_at": now, "url": result["url"], "updated_at": now})
                    save_json(posts_path, posts)
                    self._json({"ok": True, "post": post, "posts": posts, "editor": result})
                finally:
                    with PUBLISH_LOCK:
                        PUBLISHING_POSTS.discard(publish_key)
            except Exception as e:
                self._json({"error": str(e) or "네이버 블로그 발행에 실패했어요."}, 502)
            return
        if self.path == "/api/posts/draft":
            try:
                body = self._body()
                account_id = str(body.get("account_id", ""))
                post_id = str(body.get("post_id", ""))
                if not re.fullmatch(r"[0-9a-f]{32}", account_id) or not any(row.get("id") == account_id for row in _load_accounts(self)):
                    self._json({"error": "블로그 계정을 선택해 주세요."}, 400); return
                if not re.fullmatch(r"[0-9a-f]{32}", post_id):
                    self._json({"error": "에디터에 열 초안을 확인해 주세요."}, 400); return
                posts_path = data_file("blog_posts.json", account_id)
                posts = load_json(posts_path, [])
                post = next((row for row in posts if row.get("id") == post_id), None) if isinstance(posts, list) else None
                if not post:
                    self._json({"error": "에디터에 열 초안을 찾을 수 없습니다."}, 404); return
                if post.get("status") == "published":
                    self._json({"error": "이미 발행된 포스팅입니다.", "url": post.get("url", "")}, 409); return
                draft_key = (account_id, post_id)
                with PUBLISH_LOCK:
                    if draft_key in PUBLISHING_POSTS:
                        self._json({"error": "이 포스팅의 에디터 작업이 이미 진행 중이에요."}, 409); return
                    PUBLISHING_POSTS.add(draft_key)
                try:
                    from features.blog_publisher import publish_blog_post
                    result = publish_blog_post(
                        post, account_id,
                        session_prepared=bool(body.get("session_prepared")),
                        publish=False,
                    )
                    self._json({"ok": True, "post": post, "posts": posts, "editor": result})
                finally:
                    with PUBLISH_LOCK:
                        PUBLISHING_POSTS.discard(draft_key)
            except Exception as e:
                self._json({"error": str(e) or "네이버 에디터에 초안을 준비하지 못했어요."}, 502)
            return
        if self.path == "/api/posts/settings":
            try:
                body = self._body()
                account_id = str(body.get("account_id", ""))
                if not re.fullmatch(r"[0-9a-f]{32}", account_id) or not any(
                    row.get("id") == account_id for row in _load_accounts(self)
                ):
                    self._json({"error": "블로그 계정을 선택해 주세요."}, 400); return
                settings = body.get("settings")
                if not isinstance(settings, dict):
                    self._json({"error": "포스팅 설정을 확인해 주세요."}, 400); return
                mode = settings.get("writing_mode", "ai")
                style = settings.get("style", "auto")
                image_source = settings.get("image_source", "ai")
                publish_mode = settings.get("publish_mode", "now")
                image_count = settings.get("image_count", 0)
                if mode not in {"ai", "manual", "template"} or style not in {"auto", "informative", "review", "expert"}:
                    self._json({"error": "작성 방식이나 글 스타일을 확인해 주세요."}, 400); return
                if image_source not in {"ai", "upload", "free"}:
                    self._json({"error": "이미지 설정을 확인해 주세요."}, 400); return
                if publish_mode not in {"now", "scheduled"} or isinstance(image_count, bool) or not isinstance(image_count, int) or not 0 <= image_count <= 10:
                    self._json({"error": "이미지 개수나 발행 설정을 확인해 주세요."}, 400); return
                keywords = settings.get("keywords", [])
                if not isinstance(keywords, list):
                    self._json({"error": "주제와 키워드를 확인해 주세요."}, 400); return
                cleaned_keywords = [str(word).strip()[:40] for word in keywords[:10] if str(word).strip()]
                if publish_mode == "scheduled" and not str(settings.get("scheduled_at", "")).strip():
                    self._json({"error": "예약 발행 시간을 선택해 주세요."}, 400); return
                clean = {
                    "keywords": cleaned_keywords, "writing_mode": mode, "style": style, "length": "auto",
                    "images_enabled": bool(settings.get("images_enabled", True)), "image_count": image_count,
                    "image_count_auto": bool(settings.get("image_count_auto", False)),
                    "image_source": image_source, "use_cover_image": bool(settings.get("use_cover_image", True)),
                    "use_thumbnail": bool(settings.get("use_thumbnail", True)),
                    "add_hashtags": bool(settings.get("add_hashtags", True)), "add_subheading": bool(settings.get("add_subheading", True)),
                    "add_links": bool(settings.get("add_links", False)), "add_cta": bool(settings.get("add_cta", False)),
                    "publish_mode": publish_mode, "scheduled_at": str(settings.get("scheduled_at", ""))[:40],
                    "timezone": str(settings.get("timezone", "Asia/Seoul"))[:40],
                    "use_local_timezone": bool(settings.get("use_local_timezone", True)),
                }
                save_json(data_file("blog_post_settings.json", account_id), clean)
                self._json({"ok": True, "settings": {**DEFAULT_POST_SETTINGS, **clean}})
            except Exception as e:
                self._json({"error": str(e) or "포스팅 설정을 저장하지 못했어요."}, 400)
            return
        if self.path in {"/api/posts/save", "/api/posts/delete"}:
            try:
                body = self._body()
                account_id = str(body.get("account_id", ""))
                if not re.fullmatch(r"[0-9a-f]{32}", account_id) or not any(
                    row.get("id") == account_id for row in _load_accounts(self)
                ):
                    self._json({"error": "블로그 계정을 선택해 주세요."}, 400); return
                posts_path = data_file("blog_posts.json", account_id)
                posts = load_json(posts_path, [])
                if not isinstance(posts, list):
                    posts = []
                if self.path == "/api/posts/delete":
                    requested_ids = body.get("ids")
                    if isinstance(requested_ids, list):
                        post_ids = list(dict.fromkeys(str(value) for value in requested_ids))
                        if not post_ids or len(post_ids) > 200 or any(not re.fullmatch(r"[0-9a-f]{32}", post_id) for post_id in post_ids):
                            self._json({"error": "삭제할 포스팅 목록을 확인해 주세요."}, 400); return
                    else:
                        post_ids = [str(body.get("id", ""))]
                    requested = set(post_ids)
                    next_posts = [post for post in posts if post.get("id") not in requested]
                    deleted_count = len(posts) - len(next_posts)
                    if deleted_count == 0:
                        self._json({"error": "포스팅을 찾을 수 없습니다."}, 404); return
                    save_json(posts_path, next_posts)
                    self._json({"ok": True, "posts": next_posts, "deleted_count": deleted_count}); return

                title = str(body.get("title", "")).strip()[:150]
                content = str(body.get("content", "")).strip()
                category = str(body.get("category", "")).strip()[:60]
                keywords = body.get("keywords", [])
                keywords = [str(word).strip()[:40] for word in keywords[:10] if str(word).strip()] if isinstance(keywords, list) else []
                if not title:
                    self._json({"error": "포스팅 제목을 입력해 주세요."}, 400); return
                if not content:
                    self._json({"error": "포스팅 내용을 입력해 주세요."}, 400); return
                if len(content) > 100_000:
                    self._json({"error": "포스팅 내용은 10만 자까지 저장할 수 있어요."}, 400); return
                post_id = str(body.get("id", ""))
                existing = next((post for post in posts if post.get("id") == post_id), None)
                now = datetime.now().astimezone().isoformat(timespec="seconds")
                post = {
                    "id": post_id if existing else uuid.uuid4().hex,
                    "title": title,
                    "content": content,
                    "category": category,
                    "keywords": keywords,
                    "style": existing.get("style", "") if existing else "",
                    "length": existing.get("length", "") if existing else "",
                    "status": existing.get("status", "draft") if existing else "draft",
                    "created_at": existing.get("created_at", now) if existing else now,
                    "updated_at": now,
                }
                if existing:
                    if "editorial_plan" in existing:
                        post["editorial_plan"] = existing["editorial_plan"]
                    if "images" in existing:
                        post["images"] = existing["images"]
                    if "image_count_auto" in existing:
                        post["image_count_auto"] = existing["image_count_auto"]
                    if content == existing.get("content") and "blocks" in existing:
                        post["blocks"] = existing["blocks"]
                    elif existing.get("blocks"):
                        old_blocks = existing["blocks"]
                        structural = [
                            block for block in old_blocks
                            if isinstance(block, dict) and block.get("type") in {"heading", "quote"} and str(block.get("text", "")).strip()
                        ]
                        quote_texts = sorted(
                            {str(block["text"]).strip() for block in structural if block.get("type") == "quote"},
                            key=len, reverse=True,
                        )
                        heading_by_text = {
                            re.sub(r"\s+", "", str(block["text"])): block
                            for block in structural if block.get("type") == "heading"
                        }
                        rebuilt_blocks = []
                        for paragraph in content.split("\n\n"):
                            paragraph = paragraph.strip()
                            if not paragraph:
                                continue
                            heading = heading_by_text.get(re.sub(r"\s+", "", paragraph))
                            if heading:
                                rebuilt_blocks.append({**heading, "text": paragraph})
                                continue
                            matches = [quote_text for quote_text in quote_texts if quote_text in paragraph]
                            if not matches:
                                rebuilt_blocks.append({"type": "paragraph", "text": paragraph})
                                continue
                            pieces = re.split("(" + "|".join(re.escape(value) for value in matches) + ")", paragraph)
                            for piece in pieces:
                                if not piece.strip():
                                    continue
                                quote = next((block for block in structural if block.get("type") == "quote" and block.get("text", "").strip() == piece), None)
                                rebuilt_blocks.append({**quote, "text": piece} if quote else {"type": "paragraph", "text": piece})
                        text_types = {"paragraph", "heading", "quote"}
                        old_text_count = sum(isinstance(block, dict) and block.get("type") in text_types for block in old_blocks)
                        anchors = []
                        text_slot = 0
                        for block in old_blocks:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") in text_types:
                                text_slot += 1
                            elif block.get("type") in {"divider", "image", "table", "place"}:
                                anchors.append((text_slot, block))
                        for old_slot, block in reversed(anchors):
                            ratio = old_slot / max(old_text_count, 1)
                            new_slot = min(len(rebuilt_blocks), round(ratio * len(rebuilt_blocks)))
                            rebuilt_blocks.insert(new_slot, block)
                        referenced_image_ids = {block.get("image_id") for block in rebuilt_blocks if block.get("type") == "image"}
                        rebuilt_blocks.extend(
                            {"type": "image", "image_id": image.get("id"), "prompt": image.get("alt", "")}
                            for image in existing.get("images", []) if image.get("id") not in referenced_image_ids
                        )
                        post["blocks"] = rebuilt_blocks
                if not post.get("blocks"):
                    post["blocks"] = [{"type": "paragraph", "text": paragraph} for paragraph in content.split("\n\n") if paragraph.strip()]
                if post.get("blocks"):
                    from core.blog_writer import _ensure_quote_and_divider
                    post["blocks"] = _ensure_quote_and_divider(post["blocks"])
                    feature_by_type = {"paragraph":"text", "heading":"heading", "quote":"quotation", "divider":"divider", "image":"photo", "table":"table", "place":"place"}
                    for block in post["blocks"]:
                        block.setdefault("editor_feature", feature_by_type.get(block.get("type"), "text"))
                    post["editor_plan"] = [
                        {"block_index": index, "feature": block["editor_feature"], **({"style": block["style"]} if block.get("type") in {"quote", "divider"} else {})}
                        for index, block in enumerate(post["blocks"])
                    ]
                    post["quote_plan"] = [
                        {"block_index": index, "text": block.get("text", ""), "style": block.get("style", "quotation_line")}
                        for index, block in enumerate(post["blocks"]) if block.get("type") == "quote"
                    ]
                if existing:
                    posts = [post if row.get("id") == post_id else row for row in posts]
                else:
                    posts.insert(0, post)
                save_json(posts_path, posts)
                self._json({"ok": True, "post": post, "posts": posts}, 201 if not existing else 200)
            except Exception as e:
                self._json({"error": str(e) or "포스팅을 저장하지 못했어요."}, 400)
            return
        if self.path == "/api/limit-settings":
            try:
                body = self._body()
                mode = body.get("mode") if isinstance(body, dict) else None
                if mode not in {"shared", "individual"}:
                    self._json({"error": "실행 조건 방식을 확인해 주세요."}, 400)
                    return
                cfg = load_config()
                cfg["general"]["daily_limit_mode"] = mode
                if mode == "shared":
                    limit = body.get("daily_task_limit")
                    if isinstance(limit, bool) or not isinstance(limit, int) or limit not in {30, 50, 100}:
                        self._json({"error": "하루 한도는 30회, 50회, 100회 중에서 선택해 주세요."}, 400)
                        return
                    cfg["general"]["daily_task_limit"] = limit
                else:
                    limits = body.get("limits")
                    if not isinstance(limits, dict):
                        self._json({"error": "기능별 한도를 입력해 주세요."}, 400)
                        return
                    for key in ("buddy", "comment", "like"):
                        value = limits.get(key)
                        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
                            self._json({"error": "기능별 한도는 각각 0회부터 100회 사이로 입력해 주세요."}, 400)
                            return
                        cfg[key]["daily_limit"] = value
                save_config(cfg)
                self._json({"ok": True, "mode": mode,
                            "daily_task_limit": cfg["general"].get("daily_task_limit"),
                            "limits": {key: cfg[key]["daily_limit"] for key in ("buddy", "comment", "like")}})
            except Exception as e:
                self._json({"error": str(e) or "실행 한도를 저장하지 못했어요."}, 400)
            return
        if self.path == "/api/speed":
            try:
                body = self._body()
                speed = body.get("speed") if isinstance(body, dict) else None
                if speed not in {"min", "medium", "max"}:
                    self._json({"error": "작업 속도를 다시 선택해 주세요."}, 400)
                    return
                cfg = load_config()
                cfg["general"]["task_speed"] = speed
                save_config(cfg)
                self._json({"ok": True, "speed": speed})
            except Exception as e:
                self._json({"error": str(e) or "작업 속도를 저장하지 못했어요."}, 400)
            return
        if self.path != "/api/config": self._json({"error":"not found"}, 404); return
        try:
            data = self._body()
            save_config(data); self._json({"ok": True})
        except Exception as e: self._json({"error": str(e)}, 400)

def _listener_pid(port: int) -> int | None:
    """Find the process listening on the UI port using platform tools."""
    try:
        if sys.platform == "win32":
            result = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True,
                                    text=True, timeout=5, check=False)
            for line in result.stdout.splitlines():
                fields = line.split()
                if len(fields) >= 5 and fields[0].upper() == "TCP" and fields[1].endswith(f":{port}") and fields[3].upper() == "LISTENING":
                    return int(fields[4])
        else:
            result = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                                    capture_output=True, text=True, timeout=5, check=False)
            for line in result.stdout.splitlines():
                if line.strip().isdigit():
                    return int(line.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def _is_this_autosns(pid: int) -> bool:
    """Only allow replacing a server process that belongs to this app build."""
    try:
        if sys.platform == "win32":
            script = (f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}'; "
                      "if($p){$p | Select-Object ExecutablePath,CommandLine | ConvertTo-Json -Compress}")
            result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                    capture_output=True, text=True, timeout=8, check=False)
            data = json.loads(result.stdout) if result.stdout.strip() else None
            if not data:
                return False
            if getattr(sys, "frozen", False):
                return Path(data.get("ExecutablePath") or "").resolve() == Path(sys.executable).resolve()
            command = data.get("CommandLine") or ""
            return str(ROOT / "main.py").lower() in command.lower() and "--ui" in command.lower()
        result = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                capture_output=True, text=True, timeout=5, check=False)
        command = result.stdout.strip()
        if getattr(sys, "frozen", False):
            return str(Path(sys.executable).resolve()) in command
        return str(ROOT / "main.py") in command and "--ui" in command
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def _stop_previous_autosns_server(port: int = 8765) -> bool:
    pid = _listener_pid(port)
    if not pid or pid == os.getpid() or not _is_this_autosns(pid):
        return False
    try:
        if sys.platform == "win32":
            result = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=10, check=False)
            if result.returncode != 0:
                return False
        else:
            os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if _listener_pid(port) is None:
                return True
            time.sleep(0.2)
    except (OSError, subprocess.SubprocessError):
        return False
    return False


def launch():
    url = "http://127.0.0.1:8765"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    except OSError:
        # A closed browser tab does not stop the background server. Reuse a
        # compatible server; replace only an older server belonging to this app.
        try:
            with urllib.request.urlopen(f"{url}/api/session", timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if response.status == 200 and payload.get("api_version") == UI_API_VERSION:
                    try:
                        opened = webbrowser.open(url)
                    except Exception:
                        opened = False
                    if not opened and sys.platform == "win32":
                        os.startfile(url)
                    return
        except (OSError, urllib.error.URLError, ValueError):
            pass
        if _stop_previous_autosns_server():
            try:
                server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
            except OSError:
                server = None
            if server is not None:
                threading.Thread(target=server.serve_forever, daemon=True).start()
                webbrowser.open(url)
                print(f"AutoSNS 대시보드 업데이트 후 재실행: {url}")
                try:
                    threading.Event().wait()
                except KeyboardInterrupt:
                    server.shutdown()
                return
        print("AutoSNS 대시보드 포트(8765)를 사용할 수 없습니다. 기존 AutoSNS 프로세스를 종료한 뒤 다시 실행하세요.")
        if getattr(sys, "frozen", False):
            input("오류 내용을 확인한 뒤 Enter를 눌러 닫으세요.")
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    webbrowser.open(url)
    print(f"AutoSNS 대시보드 실행 중: {url} (종료: Ctrl+C)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()
