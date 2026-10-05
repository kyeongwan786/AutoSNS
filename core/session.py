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
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, Playwright

from core.config import BASE_DIR
from core.storage import load_json, save_json

PROFILE_DIR = BASE_DIR / "naver_profile"        # playwright 모드 프로필 (공유 금지!)
CHROME_PROFILE_DIR = BASE_DIR / "chrome_profile"  # cdp 모드 전용 크롬 프로필 (공유 금지!)
COOKIE_FILE = BASE_DIR / "naver_cookies.json"   # 쿠키 백업 (공유 금지!)

LOGIN_COOKIES = {"NID_AUT", "NID_SES"}
LOGIN_WAIT_SECONDS = 300
LOGIN_URL = "https://nid.naver.com/nidlogin.login"
# 로그아웃 상태면 반드시 로그인 페이지로 튕기는 네이버 메일로 실제 로그인 여부 확인
LOGIN_CHECK_URL = "https://mail.naver.com/"

# 현재 열린 세션 정보 (close_naver 에서 정리)
_state: dict = {"mode": None, "browser": None, "context": None, "proc": None, "port": 9222}


class LoginCompletedError(Exception):
    """Raised when login was completed but the user must start work in the UI."""


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


async def backup_cookies(context: BrowserContext) -> None:
    cookies = [c for c in await context.cookies() if "naver.com" in c.get("domain", "")]
    if LOGIN_COOKIES.issubset({c["name"] for c in cookies}):
        save_json(COOKIE_FILE, cookies)


async def restore_cookies(context: BrowserContext) -> bool:
    if not COOKIE_FILE.exists():
        return False
    try:
        await context.add_cookies(load_json(COOKIE_FILE, []))
    except Exception as e:
        print(f"쿠키 복원 실패: {e}")
        return False
    return await is_logged_in(context)


def forget_cookies() -> None:
    if COOKIE_FILE.exists():
        COOKIE_FILE.unlink()


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
    CHROME_PROFILE_DIR.mkdir(exist_ok=True)
    args = [
        chrome,
        f"--user-data-dir={CHROME_PROFILE_DIR}",
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
        args.append("--window-position=-3000,-3000")   # 창을 화면 밖으로
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
        proc = _start_chrome(chrome, port, visible)
        await _wait_port(port)
    browser, context, page = await _cdp_connect(p, port)
    _state.update(browser=browser, context=context, proc=proc)
    return context, page


async def _open_cdp(p: Playwright, g: dict,
                    stop_after_login: bool = False) -> tuple[BrowserContext, Page]:
    chrome = find_chrome(g.get("chrome_path", ""))
    port = int(g.get("cdp_port", 9222))
    visible = g.get("show_browser", False)
    _state.update(mode="cdp", port=port)

    print("🔎 로그인 상태 확인 중...")
    context, page = await _cdp_launch(p, chrome, port, visible)
    if await check_login(page):
        print("✅ 저장된 세션으로 로그인 상태입니다.")
        await backup_cookies(context)
        return context, page

    print("❌ 로그아웃 상태입니다.")
    await _cdp_shutdown()

    # --- 사람이 직접 로그인: 디버깅 포트 없이 완전히 일반 크롬으로 ---
    for attempt in range(3):
        print("\n🔐 크롬 창에서 직접 로그인해 주세요. ('로그인 상태 유지' 꼭 체크)")
        login_proc = _start_chrome(chrome, port, visible=True, url=LOGIN_URL,
                                   debug=False, maximize=True)
        await asyncio.to_thread(input, "   로그인을 마쳤으면 크롬 창을 닫고 여기서 Enter 를 누르세요: ")
        try:
            login_proc.wait(timeout=15)
        except Exception:
            print("   ⚠️ 크롬이 아직 켜져 있어서 종료합니다.")
            login_proc.terminate()
            await asyncio.sleep(2)

        context, page = await _cdp_launch(p, chrome, port, visible)
        if await check_login(page):
            await backup_cookies(context)
            if stop_after_login:
                print("✅ 로그인 정보 저장 완료. 대시보드에서 '작업 시작'을 눌러주세요.")
                await _cdp_shutdown()
                raise LoginCompletedError
            print("✅ 로그인 확인 완료.")
            return context, page
        print("❌ 아직 로그인이 확인되지 않습니다. 다시 시도해 주세요.")
        await _cdp_shutdown()

    raise RuntimeError("로그인 확인에 3번 실패했습니다.")


# ============================================================
# playwright 모드 (예전 방식)
# ============================================================

async def _pw_launch(p: Playwright, headless: bool, use_chrome: bool) -> tuple[BrowserContext, Page]:
    PROFILE_DIR.mkdir(exist_ok=True)
    context = await p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=headless,
        locale="ko-KR",
        viewport={"width": 1280, "height": 900},
        channel="chrome" if use_chrome else None,
    )
    page = context.pages[0] if context.pages else await context.new_page()
    _state.update(context=context)
    return context, page


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
                     stop_after_login: bool = False) -> tuple[BrowserContext, Page]:
    if general.get("browser_mode", "cdp") == "playwright":
        return await _open_playwright(p, general, stop_after_login)
    return await _open_cdp(p, general, stop_after_login)


async def close_naver() -> None:
    if _state["mode"] == "cdp":
        await _cdp_shutdown()
    elif _state["context"] is not None:
        try:
            await _state["context"].close()
        except Exception:
            pass
        _state["context"] = None
