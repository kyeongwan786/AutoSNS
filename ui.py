"""AutoSNS dashboard launcher. Run with: python main.py --ui"""
from __future__ import annotations
import json
import hashlib
import hmac
import os
import secrets
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from datetime import date, datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from core.config import BASE_DIR, CONFIG_FILE, RESOURCE_DIR, load_config, save_config
from core import cloud
from core.llm import resolve_api_key
from core.storage import ACTIVITY_FILE, TARGETS_FILE, DAILY_FILE, load_json

ROOT = RESOURCE_DIR
PAGE = ROOT / "dashboard.html"
AUTH_USERS_FILE = BASE_DIR / "auth_users.json"
AUTH_ITERATIONS = 160_000
AUTH_LOCK = threading.Lock()
AUTH_SESSIONS: dict[str, dict] = {}

FEATURE_LABELS = {"buddy": "서이추", "comment": "댓글", "like": "공감"}
RUN_LOCK = threading.Lock()
RUN_PROCESS: subprocess.Popen | None = None
RUN_MODE: str | None = None


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
    token = _session_from_request(handler)
    with AUTH_LOCK:
        return token in AUTH_SESSIONS if token else False


def _auth_session(handler) -> dict | None:
    token = _session_from_request(handler)
    with AUTH_LOCK:
        return AUTH_SESSIONS.get(token) if token else None


def _activity_rows() -> list[dict]:
    rows = load_json(ACTIVITY_FILE, [])
    if not isinstance(rows, list):
        rows = []

    # 기존 targets.json에는 각 대상의 마지막 처리 결과가 남아 있으므로
    # 새 활동 로그가 만들어지기 전에도 실제 완료 기록은 표시할 수 있습니다.
    seen = {
        (r.get("blog_id"), r.get("feature"), r.get("status"),
         str(r.get("at", ""))[:10], r.get("detail", ""))
        for r in rows if isinstance(r, dict)
    }
    targets = load_json(TARGETS_FILE, {})
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


def blog_state() -> dict:
    cfg = load_config()
    daily = load_json(DAILY_FILE, {})
    counts = daily.get(str(date.today()), {}) if isinstance(daily, dict) else {}
    if isinstance(counts, int):
        counts = {"buddy": counts}
    if not isinstance(counts, dict):
        counts = {}

    limits = {
        "buddy": int(cfg.get("buddy", {}).get("daily_limit", 0)),
        "comment": int(cfg.get("comment", {}).get("daily_limit", 0)),
        "like": int(cfg.get("like", {}).get("daily_limit", 0)),
    }
    labels = {"buddy": "서이추", "comment": "댓글", "like": "공감"}
    tasks = [
        {"feature": feature, "label": labels[feature],
         "count": int(counts.get(feature, 0)), "limit": limits[feature]}
        for feature in ("buddy", "comment", "like")
    ]
    total = sum(item["count"] for item in tasks)
    total_limit = sum(item["limit"] for item in tasks)

    rows = _activity_rows()
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

    return {
        "activity": activity,
        "activity_total": len(rows),
        "tasks": tasks,
        "total": total,
        "total_limit": total_limit,
        "progress": round(total * 100 / total_limit) if total_limit else 0,
        "templates": template_rows,
        "features": {
            feature: bool(cfg.get(feature, {}).get("enabled", False))
            for feature in FEATURE_LABELS
        },
        # 현재 자동화에는 포스팅 발행 및 키워드 전환 기록 기능이 없습니다.
        "posts": [],
        "keywords": [],
    }

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def _json(self, obj, code=200, headers=None):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
        for key, value in (headers or {}).items(): self.send_header(key, value)
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def _body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
    def do_GET(self):
        global RUN_MODE
        if self.path == "/api/session":
            session = _auth_session(self)
            self._json({"authenticated": session is not None,
                        "username": session.get("email") if session else None,
                        "cloud_auth": cloud.supabase_configured()}); return
        if self.path.startswith("/api/") and not _is_authenticated(self):
            self._json({"error": "로그인이 필요합니다."}, 401); return
        if self.path == "/api/state":
            cfg = load_config(); cfg.get("comment", {})["api_key"] = ""
            targets = load_json(TARGETS_FILE, {}); daily = load_json(DAILY_FILE, {})
            self._json({"config": cfg, "targets": targets, "daily": daily}); return
        if self.path == "/api/blog":
            self._json(blog_state()); return
        if self.path == "/api/run-status":
            with RUN_LOCK:
                process = RUN_PROCESS
                running = process is not None and process.poll() is None
                self._json({"running": running,
                            "mode": RUN_MODE,
                            "exit_code": None if running or process is None else process.returncode})
            return
        if self.path == "/api/update-check":
            self._json(cloud.check_update()); return
        data = PAGE.read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_POST(self):
        global RUN_PROCESS, RUN_MODE
        if self.path in {"/api/auth/login", "/api/auth/signup"}:
            try:
                body = self._body()
                username = str(body.get("email", body.get("username", ""))).strip()
                password = str(body.get("password", ""))
                if not username or not password:
                    self._json({"error": "이메일과 비밀번호를 입력하세요."}, 400); return
                if cloud.supabase_configured():
                    if self.path == "/api/auth/signup":
                        cloud.signup(username, password)
                        self._json({"ok": True, "signup_pending": True,
                                    "message": "가입 신청이 완료됐습니다. 이메일 인증 후 관리자가 사용 권한을 활성화하면 로그인할 수 있습니다."}, 201)
                        return
                    account = cloud.login(username, password)
                    token = secrets.token_urlsafe(32)
                    with AUTH_LOCK:
                        AUTH_SESSIONS[token] = account
                    self._json({"ok": True, "username": account["email"]}, headers={
                        "Set-Cookie": f"autosns_session={token}; Path=/; HttpOnly; SameSite=Lax"
                    })
                    return
                with AUTH_LOCK:
                    users = _load_auth_users()
                    if self.path == "/api/auth/signup":
                        if len(username) > 254 or len(password) < 4:
                            self._json({"error": "이메일은 254자 이하, 비밀번호는 4자 이상이어야 합니다."}, 400); return
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
                self._json({"error": str(e)}, 400)
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
            self._json({"error": "로그인이 필요합니다."}, 401); return
        if self.path in {"/api/run", "/api/login"}:
            login_only = self.path == "/api/login"
            session = _auth_session(self)
            if not login_only and cloud.supabase_configured() and session:
                try:
                    cloud.check_license(session["access_token"], session["user_id"])
                except cloud.CloudError as e:
                    self._json({"error": str(e)}, 403)
                    return
            with RUN_LOCK:
                if RUN_PROCESS is not None and RUN_PROCESS.poll() is None:
                    self._json({"running": True, "message": "다른 작업이 이미 실행 중입니다."}, 409)
                    return
                try:
                    if getattr(sys, "frozen", False):
                        command = [sys.executable, "--login-only" if login_only else "--automation-child"]
                    else:
                        command = [sys.executable, str(ROOT / "main.py")]
                        if login_only:
                            command.append("--login-only")
                    env = os.environ.copy()
                    if session and session.get("access_token"):
                        env["AUTOSNS_SUPABASE_ACCESS_TOKEN"] = session["access_token"]
                        env["AUTOSNS_SUPABASE_REFRESH_TOKEN"] = session.get("refresh_token", "")
                    RUN_PROCESS = subprocess.Popen(
                        command,
                        cwd=str(BASE_DIR),
                        env=env,
                    )
                    RUN_MODE = "login" if login_only else "work"
                except Exception as e:
                    self._json({"error": str(e)}, 500)
                    return
            message = ("네이버 로그인 준비를 시작했습니다. 로그인 후 작업 시작을 눌러주세요."
                       if login_only else "작업을 시작했습니다.")
            self._json({"running": True, "message": message})
            return
        if self.path == "/api/feature":
            try:
                body = self._body()
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
        if self.path != "/api/config": self._json({"error":"not found"}, 404); return
        try:
            data = self._body()
            save_config(data); self._json({"ok": True})
        except Exception as e: self._json({"error": str(e)}, 400)

def launch():
    url = "http://127.0.0.1:8765"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    except OSError:
        # The browser window can be closed while this process keeps serving.
        # Reuse that server on a later EXE launch instead of starting a second
        # process that fails to bind the port and leaves the user without a UI.
        try:
            with urllib.request.urlopen(f"{url}/api/session", timeout=2) as response:
                if response.status == 200:
                    try:
                        opened = webbrowser.open(url)
                    except Exception:
                        opened = False
                    if not opened and sys.platform == "win32":
                        os.startfile(url)
                    return
        except (OSError, urllib.error.URLError):
            pass
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
