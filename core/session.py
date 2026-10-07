"""
네이버 로그인 세션.

browser_mode (config.json 의 general):
  "cdp"        : PC에 설치된 일반 크롬을 자동화 표시 없이 직접 실행하고, 프로그램이 나중에 연결 (기본, 캡챠 적음)
  "playwright" : Playwright 내장 브라우저 (예전 방식)

cdp 모드 흐름:
  1) chrome_profile 프로필로 크롬 실행 → 연결해서 로그인 확인
  2) 로그아웃이면 크롬을 '연결 없이' 로그인 화면으로 띄움 → 사람이 직접 로그인 → 터미널에서 Enter
  3) 로그인 확인되면 크롬 재실행 후 연결해서 작업 (show_browser=False면 창을 화면 밖으로)
"""
from __future__ import annotations
import asyncio
import ctypes
import ctypes.wintypes
from contextvars import ContextVar
import hashlib
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, Playwright

from core.config import BASE_DIR
from core.storage import load_json, save_json

_ACCOUNT_ID_CONTEXT: ContextVar[str] = ContextVar("autosns_account_id", default="")


def _active_account_id() -> str:
    return _ACCOUNT_ID_CONTEXT.get() or os.environ.get("AUTOSNS_ACCOUNT_ID", "")

def _account_storage(name: str) -> Path:
    account_id = _active_account_id()
    if account_id and len(account_id) == 32 and all(ch in "0123456789abcdef" for ch in account_id):
        path = BASE_DIR / "sns_accounts" / account_id / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return BASE_DIR / name

PROFILE_DIR = BASE_DIR / "naver_profile"
CHROME_PROFILE_DIR = BASE_DIR / "chrome_profile"
COOKIE_FILE = BASE_DIR / "naver_cookies.json"

def _profile_dir() -> Path:
    return _account_storage("naver_profile") if _active_account_id() else PROFILE_DIR

def _chrome_profile_dir() -> Path:
    return _account_storage("chrome_profile") if _active_account_id() else CHROME_PROFILE_DIR

def _cookie_file() -> Path:
    return _account_storage("naver_cookies.json") if _active_account_id() else COOKIE_FILE

def _cdp_port(general: dict) -> int:
    account_id = _active_account_id()
    if not account_id:
        return int(general.get("cdp_port", 9222))
    offset = int(hashlib.sha256(account_id.encode("ascii")).hexdigest()[:8], 16) % 10000
    return 12000 + offset

LOGIN_COOKIES = {"NID_AUT", "NID_SES"}
LOGIN_WAIT_SECONDS = 300
LOGIN_URL = "https://nid.naver.com/nidlogin.login"
# 로그아웃 상태면 반드시 로그인 페이지로 튕기는 네이버 메일로 실제 로그인 여부 확인
LOGIN_CHECK_URL = "https://mail.naver.com/"

# 현재 열린 세션 정보 (close_naver 에서 정리)
_state: dict = {"mode": None, "browser": None, "context": None, "proc": None, "port": 9222}


class LoginCompletedError(Exception):
    """Raised when login was completed but the user must start work in the UI."""


class NaverCredentialError(Exception):
    """Naver explicitly rejected the submitted username or password."""


async def _credential_error_visible(page: Page) -> bool:
    selectors = ("#err_common", ".error_message", "#id_error", "#pw_error", ".login_error")
    invalid_phrases = (
        "아이디 또는 비밀번호", "아이디나 비밀번호", "비밀번호가 올바르지", "비밀번호가 잘못",
        "아이디가 존재하지", "존재하지 않는 아이디", "로그인 정보가 올바르지",
        "incorrect password", "invalid username", "account does not exist",
    )
    for selector in selectors:
        matches = page.locator(selector)
        try:
            count = min(await matches.count(), 4)
            for index in range(count):
                item = matches.nth(index)
                if not await item.is_visible():
                    continue
                message = (await item.inner_text(timeout=500)).strip().casefold()
                if any(phrase.casefold() in message for phrase in invalid_phrases):
                    return True
        except Exception:
            continue
    return False


# ============================================================
# 공통: 로그인 확인 / 쿠키 백업
# ============================================================

async def is_logged_in(context: BrowserContext) -> bool:
    """쿠키가 있는지만 보는 빠른 확인 (실제 로그인 보장 X)"""
    cookies = await context.cookies("https://www.naver.com")
    return LOGIN_COOKIES.issubset({c["name"] for c in cookies})


async def check_login(page: Page) -> bool:
    """네이버 메일에 들어가서 실제 로그인 상태인지 확인"""
    try:
        await page.goto(LOGIN_CHECK_URL, wait_until="domcontentloaded")
        for _ in range(6):
            if "nidlogin" in page.url:
                return False
            await page.wait_for_timeout(500)
    except Exception:
        return False
    return "nidlogin" not in page.url and "mail.naver.com" in page.url


async def _check_context_login(context: BrowserContext) -> bool:
    """Verify auth on a probe tab without navigating away from a login challenge."""
    page = await context.new_page()
    try:
        return await check_login(page)
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def backup_cookies(context: BrowserContext) -> None:
    cookies = [c for c in await context.cookies() if "naver.com" in c.get("domain", "")]
    if LOGIN_COOKIES.issubset({c["name"] for c in cookies}):
        save_json(_cookie_file(), cookies)


async def restore_cookies(context: BrowserContext) -> bool:
    cookie_file = _cookie_file()
    if not cookie_file.exists():
        return False
    try:
        await context.add_cookies(load_json(cookie_file, []))
    except Exception as e:
        print(f"쿠키 복원 실패: {e}")
        return False
    return await is_logged_in(context)


def forget_cookies() -> None:
    cookie_file = _cookie_file()
    if cookie_file.exists():
        cookie_file.unlink()


# ============================================================
# cdp 모드: 일반 크롬 직접 실행 + 연결
# ============================================================

def find_chrome(custom: str = "") -> str:
    candidates = [custom] if custom else []
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA", "")
        candidates += [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
        ]
    elif sys.platform == "darwin":
        candidates += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    else:
        candidates += ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"]
    for c in candidates:
        if c and Path(c).exists():
            return c
    raise RuntimeError("크롬을 찾을 수 없습니다. config.json 의 general.chrome_path 에 chrome.exe 경로를 넣어주세요.")


def _port_alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1):
            return True
    except Exception:
        return False


def _start_chrome(chrome: str, port: int, visible: bool, url: str = "about:blank",
                  debug: bool = True, maximize: bool = False) -> subprocess.Popen:
    chrome_profile_dir = _chrome_profile_dir()
    chrome_profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        chrome,
        f"--user-data-dir={chrome_profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--lang=ko-KR",
        # Keep visible/login windows within common laptop display bounds.
        "--window-size=1100,680",
    ]
    if debug:
        args.append(f"--remote-debugging-port={port}")
    if visible:
        # Chrome remembers the last position for this profile. Explicitly put
        # login windows back on the primary monitor after hidden runs.
        args.append("--window-position=0,0")
        if maximize:
            args.append("--start-maximized")
    else:
        # Off-screen window positioning is ignored by macOS Chrome. Use modern
        # headless mode so show_browser=False actually keeps automation hidden.
        args.append("--headless=new")
    args.append(url)
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if maximize and sys.platform == "win32":
        _bring_process_window_forward(proc)
    return proc


def _bring_process_window_forward(proc: subprocess.Popen) -> None:
    """Wait for this Chrome process's window, then maximize and foreground it."""
    user32 = ctypes.windll.user32
    deadline = time.monotonic() + 8
    callback_type = ctypes.WINFUNCTYPE(
        ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
    )
    user32.GetWindowThreadProcessId.argtypes = [
        ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.DWORD)
    ]
    user32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD
    user32.IsWindowVisible.argtypes = [ctypes.wintypes.HWND]
    user32.IsWindowVisible.restype = ctypes.wintypes.BOOL
    user32.EnumWindows.argtypes = [callback_type, ctypes.wintypes.LPARAM]
    user32.EnumWindows.restype = ctypes.wintypes.BOOL
    user32.ShowWindow.argtypes = [ctypes.wintypes.HWND, ctypes.c_int]
    user32.BringWindowToTop.argtypes = [ctypes.wintypes.HWND]
    user32.SetForegroundWindow.argtypes = [ctypes.wintypes.HWND]

    while time.monotonic() < deadline:
        windows: list[int] = []

        def collect(hwnd, _lparam):
            pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == proc.pid and user32.IsWindowVisible(hwnd):
                windows.append(hwnd)
            return True

        callback = callback_type(collect)
        user32.EnumWindows(callback, 0)
        if windows:
            hwnd = windows[0]
            user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            return
        if proc.poll() is not None:
            return
        time.sleep(0.2)


async def _wait_port(port: int, timeout: float = 20) -> None:
    for _ in range(int(timeout * 2)):
        if _port_alive(port):
            return
        await asyncio.sleep(0.5)
    raise RuntimeError("크롬 연결 대기 시간 초과. 같은 프로필의 크롬이 이미 켜져 있다면 모두 닫고 다시 실행하세요.")


async def _cdp_connect(p: Playwright, port: int) -> tuple[Browser, BrowserContext, Page]:
    browser = await p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = context.pages[0] if context.pages else await context.new_page()
    return browser, context, page


async def _cdp_shutdown() -> None:
    """연결된 크롬을 정상 종료 (쿠키가 디스크에 저장되도록)"""
    browser: Browser | None = _state["browser"]
    proc: subprocess.Popen | None = _state["proc"]
    if browser is not None:
        try:
            cdp = await browser.new_browser_cdp_session()
            await cdp.send("Browser.close")
        except Exception:
            pass
    if proc is not None:
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.terminate()
    # 포트가 닫힐 때까지 잠깐 대기
    for _ in range(20):
        if not _port_alive(_state["port"]):
            break
        await asyncio.sleep(0.5)
    _state.update(browser=None, context=None, proc=None)


async def _cdp_launch(p: Playwright, chrome: str, port: int, visible: bool) -> tuple[BrowserContext, Page]:
    if _port_alive(port):
        print(f"ℹ️ 이미 켜진 크롬(포트 {port})에 연결합니다.")
        proc = None
    else:
        proc = _start_chrome(chrome, port, visible, maximize=visible)
        await _wait_port(port)
    browser, context, page = await _cdp_connect(p, port)
    if visible:
        try:
            cdp = await browser.new_browser_cdp_session()
            window = await cdp.send("Browser.getWindowForTarget")
            await cdp.send("Browser.setWindowBounds", {
                "windowId": window["windowId"],
                "bounds": {"windowState": "maximized"},
            })
            await cdp.detach()
        except Exception:
            pass
    _state.update(browser=browser, context=context, proc=proc)
    return context, page


async def _open_cdp_with_credentials(p: Playwright, g: dict, credentials: dict) -> tuple[BrowserContext, Page]:
    """Check the saved profile, then use paced credential entry if logged out."""
    chrome = find_chrome(g.get("chrome_path", ""))
    port = _cdp_port(g)
    visible = bool(g.get("show_browser", False))
    _state.update(mode="cdp", port=port)
    login_proc = None
    try:
        if _port_alive(port):
            context, page = await _cdp_launch(p, chrome, port, visible=visible)
        else:
            context, page = await _cdp_launch(p, chrome, port, visible=visible)

        # Check the existing account profile in this same visible window.
        if await check_login(page):
            await backup_cookies(context)
            await _cdp_shutdown()
            raise LoginCompletedError

        # The browser profile can be missing its persisted session even when
        # this account has a cookie backup (for example after its profile was
        # reset). Restore only this account's cookies, then verify with Naver.
        if await restore_cookies(context) and await check_login(page):
            await backup_cookies(context)
            await _cdp_shutdown()
            raise LoginCompletedError

        # Only open a visible window when the saved profile and cookies are
        # actually logged out. After manual verification, close it and let the
        # caller reopen this same account profile in its configured mode.
        await _cdp_shutdown()
        login_proc = _start_chrome(chrome, port, visible=True, url=LOGIN_URL,
                                   debug=True, maximize=True)
        await _wait_port(port)
        browser, context, page = await _cdp_connect(p, port)
        _state.update(mode="cdp", port=port, browser=browser, context=context, proc=login_proc)

        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(1200)
        id_field = page.locator("#id")
        if not await id_field.count():
            id_field = page.locator('input[name="id"]')
        password_field = page.locator("#pw")
        if not await password_field.count():
            password_field = page.locator('input[name="pw"]')

        # Use paced keyboard input and short pauses instead of filling both
        # fields in one immediate automation action. CAPTCHA remains manual.
        await id_field.first.click()
        await id_field.first.fill("")
        await page.keyboard.type(str(credentials.get("username", "")), delay=95)
        await page.wait_for_timeout(850)
        await password_field.first.click()
        await password_field.first.fill("")
        await page.keyboard.type(str(credentials.get("password", "")), delay=105)
        credentials["username"] = ""
        credentials["password"] = ""
        await page.wait_for_timeout(1100)

        login_button = page.locator("#log\\.login")
        if not await login_button.count():
            login_button = page.locator('button[type="submit"], input[type="submit"]')
        submitted = False
        try:
            if await login_button.count():
                await login_button.first.click(timeout=7000)
                submitted = True
        except Exception:
            pass
        if not submitted:
            try:
                await password_field.first.press("Enter", timeout=5000)
                submitted = True
            except Exception:
                pass
        if not submitted:
            form = page.locator("#frmNIDLogin")
            if await form.count():
                await form.evaluate("form => form.requestSubmit()")
                submitted = True
        if not submitted:
            raise RuntimeError("네이버 로그인 요청을 제출하지 못했습니다.")

        for _ in range(LOGIN_WAIT_SECONDS):
            if await _credential_error_visible(page):
                raise NaverCredentialError("Naver rejected the submitted credentials")
            if await is_logged_in(context) and await _check_context_login(context):
                await backup_cookies(context)
                await _cdp_shutdown()
                raise LoginCompletedError
            if not _port_alive(port):
                break
            await asyncio.sleep(1)
        raise RuntimeError("네이버 로그인을 확인하지 못했어요. 열린 창에서 본인 확인을 마친 뒤 다시 시도해 주세요.")
    except (LoginCompletedError, NaverCredentialError):
        raise
    except Exception as exc:
        raise RuntimeError("네이버 로그인에 실패했어요. 열린 창에서 로그인 또는 본인 확인을 완료해 주세요.") from exc
    finally:
        credentials["username"] = ""
        credentials["password"] = ""
        if _state.get("browser") is not None or _state.get("proc") is not None:
            await _cdp_shutdown()
        elif login_proc is not None and login_proc.poll() is None:
            login_proc.terminate()


async def _open_cdp(p: Playwright, g: dict,
                    stop_after_login: bool = False,
                    credentials: dict | None = None) -> tuple[BrowserContext, Page]:
    if credentials is not None:
        return await _open_cdp_with_credentials(p, g, credentials)
    chrome = find_chrome(g.get("chrome_path", ""))
    port = _cdp_port(g)
    visible = g.get("show_browser", False)
    _state.update(mode="cdp", port=port)

    print("🔎 로그인 상태 확인 중...")
    context, page = await _cdp_launch(p, chrome, port, visible)
    if await check_login(page):
        print("✅ 저장된 세션으로 로그인 상태입니다.")
        await backup_cookies(context)
        return context, page

    # Each account has its own cookie backup. Use it if the Chrome profile's
    # session was cleared or did not persist, and validate it against Naver
    # before treating the account as logged in.
    if await restore_cookies(context) and await check_login(page):
        print("✅ 계정 쿠키 백업으로 로그인 상태를 복원했습니다.")
        await backup_cookies(context)
        return context, page

    print("❌ 로그아웃 상태입니다.")
    await _cdp_shutdown()

    # --- 사람이 직접 로그인: 디버깅 포트 없이 완전히 일반 크롬으로 ---
    # A UI login attempt must not keep relaunching Chrome after the user closes
    # the login window. They can retry from the dashboard if verification fails.
    max_attempts = 1 if stop_after_login else 3
    for attempt in range(max_attempts):
        print("\n🔐 열린 크롬 창에서 네이버 로그인해 주세요. 로그인 창을 닫으면 자동으로 확인합니다.")
        login_proc = _start_chrome(chrome, port, visible=True, url=LOGIN_URL,
                                   debug=False, maximize=True)
        for _ in range(LOGIN_WAIT_SECONDS):
            if login_proc.poll() is not None:
                break
            await asyncio.sleep(1)
        else:
            print("   ⚠️ 로그인 대기 시간이 끝났습니다.")
            login_proc.terminate()
            await asyncio.sleep(2)
            raise TimeoutError("네이버 로그인 대기 시간이 초과되었습니다.")
        try:
            login_proc.wait(timeout=5)
        except Exception:
            pass

        context, page = await _cdp_launch(p, chrome, port, visible)
        if await check_login(page):
            await backup_cookies(context)
            if stop_after_login:
                print("✅ 로그인 정보 저장 완료. 대시보드에서 '작업 시작'을 눌러주세요.")
                await _cdp_shutdown()
                raise LoginCompletedError
            print("✅ 로그인 확인 완료.")
            return context, page
        print("❌ 아직 로그인이 확인되지 않습니다.")
        await _cdp_shutdown()

    raise RuntimeError("네이버 로그인을 확인하지 못했습니다. 대시보드에서 다시 시도해 주세요.")


# ============================================================
# playwright 모드 (예전 방식)
# ============================================================

async def _pw_launch(p: Playwright, headless: bool, use_chrome: bool) -> tuple[BrowserContext, Page]:
    profile_dir = _profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)
    context = await p.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=headless,
        locale="ko-KR",
        viewport=None if not headless else {"width": 1280, "height": 900},
        args=["--start-maximized"] if not headless else [],
        channel="chrome" if use_chrome else None,
    )
    page = context.pages[0] if context.pages else await context.new_page()
    _state.update(context=context)
    return context, page


async def _open_playwright_with_credentials(p: Playwright, g: dict,
                                            stop_after_login: bool,
                                            credentials: dict) -> tuple[BrowserContext, Page]:
    """Use the account's saved credentials when its persistent profile expires."""
    context = None
    try:
        context, page = await _pw_launch(p, headless=False, use_chrome=g.get("use_chrome", False))
        if await is_logged_in(context) and await check_login(page):
            await backup_cookies(context)
            raise LoginCompletedError
        if await restore_cookies(context) and await check_login(page):
            await backup_cookies(context)
            raise LoginCompletedError

        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(800)
        id_field = page.locator("#id")
        if not await id_field.count():
            id_field = page.locator('input[name="id"]')
        password_field = page.locator("#pw")
        if not await password_field.count():
            password_field = page.locator('input[name="pw"]')
        if not await id_field.count() or not await password_field.count():
            raise RuntimeError("네이버 로그인 입력란을 찾지 못했어요.")
        await id_field.first.fill(str(credentials.get("username", "")))
        await password_field.first.fill(str(credentials.get("password", "")))
        credentials["username"] = ""
        credentials["password"] = ""
        login_button = page.locator("#log\\.login")
        if not await login_button.count():
            login_button = page.locator('button[type="submit"], input[type="submit"]')
        if await login_button.count():
            await login_button.first.click(timeout=8_000)
        else:
            await password_field.first.press("Enter", timeout=5_000)

        for _ in range(LOGIN_WAIT_SECONDS):
            if await _credential_error_visible(page):
                raise NaverCredentialError("네이버가 저장된 아이디 또는 비밀번호를 거부했습니다. 계정 연결에서 로그인 정보를 갱신해 주세요.")
            if await is_logged_in(context) and await _check_context_login(context):
                await backup_cookies(context)
                raise LoginCompletedError
            await page.wait_for_timeout(1_000)
        raise TimeoutError("네이버 로그인 확인 시간이 초과됐어요. 열린 브라우저에서 본인 확인을 마친 뒤 다시 시도해 주세요.")
    except (LoginCompletedError, NaverCredentialError):
        raise
    finally:
        credentials["username"] = ""
        credentials["password"] = ""
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
            if _state.get("context") is context:
                _state["context"] = None


async def _open_playwright(p: Playwright, g: dict,
                           stop_after_login: bool = False) -> tuple[BrowserContext, Page]:
    show = g.get("show_browser", False)
    use_chrome = g.get("use_chrome", False)
    _state.update(mode="playwright")

    print("🔎 로그인 상태 확인 중...")
    context, page = await _pw_launch(p, headless=not show, use_chrome=use_chrome)
    if await is_logged_in(context) and await check_login(page):
        print("✅ 저장된 세션으로 로그인 상태입니다.")
        await backup_cookies(context)
        return context, page
    if await restore_cookies(context) and await check_login(page):
        print("✅ 백업한 쿠키로 로그인 상태를 복원했습니다.")
        return context, page

    print("❌ 로그아웃 상태입니다.")
    forget_cookies()
    await context.clear_cookies()
    if not show:
        await context.close()
        context, page = await _pw_launch(p, headless=False, use_chrome=use_chrome)

    print("🔐 브라우저 창에서 직접 로그인해 주세요. ('로그인 상태 유지' 꼭 체크)")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded")
    for _ in range(LOGIN_WAIT_SECONDS):
        if await is_logged_in(context) and "nidlogin" not in page.url:
            break
        await page.wait_for_timeout(1_000)
    else:
        await context.close()
        raise RuntimeError("로그인 대기 시간이 초과되었습니다.")

    await backup_cookies(context)
    print("✅ 로그인 완료.")
    if stop_after_login:
        await context.close()
        _state["context"] = None
        raise LoginCompletedError
    if not show:
        await context.close()
        context, page = await _pw_launch(p, headless=True, use_chrome=use_chrome)
        if not await check_login(page):
            await restore_cookies(context)
    return context, page


# ============================================================
# 외부에서 쓰는 함수
# ============================================================

async def open_naver(p: Playwright, general: dict,
                     stop_after_login: bool = False,
                     credentials: dict | None = None,
                     account_id: str | None = None) -> tuple[BrowserContext, Page]:
    token = _ACCOUNT_ID_CONTEXT.set(account_id) if account_id else None
    try:
        if general.get("browser_mode", "cdp") == "playwright":
            if credentials is not None:
                return await _open_playwright_with_credentials(p, general, stop_after_login, credentials)
            return await _open_playwright(p, general, stop_after_login)
        return await _open_cdp(p, general, stop_after_login, credentials)
    finally:
        if token is not None:
            _ACCOUNT_ID_CONTEXT.reset(token)


async def close_naver() -> None:
    if _state["mode"] == "cdp":
        await _cdp_shutdown()
    elif _state["context"] is not None:
        try:
            await _state["context"].close()
        except Exception:
            pass
        _state["context"] = None
