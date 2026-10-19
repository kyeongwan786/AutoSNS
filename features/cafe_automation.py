"""Visible Naver Cafe attendance and word-chain posting actions."""
from __future__ import annotations

import asyncio
import contextvars
import json
import re
import threading
from datetime import datetime

from playwright.async_api import async_playwright

from core.config import load_config
from core.credentials import load_naver_credentials
from core.session import close_naver
from core.storage import account_data_dir

CAFE_HOME = "https://cafe.naver.com/skysky123"
ATTENDANCE_URL = "https://cafe.naver.com/skysky123?iframe_url_utf8=%2FAttendanceView.nhn%253Fsearch.clubid%3D30136794%2526search.menuid%3D45"
WORD_CHAIN_LIST_URL = "https://cafe.naver.com/f-e/cafes/30136794/menus/59?viewType=L"
WORD_CHAIN_WRITE_URL = "https://cafe.naver.com/ca-fe/cafes/30136794/menus/59/articles/write?boardType=L"
_LOG_ACCOUNT_ID = contextvars.ContextVar("cafe_log_account_id", default="")
_LOG_LOCK = threading.Lock()
_LOG_MAX_BYTES = 1_000_000


def log_cafe_event(account_id: str, event: str, **details) -> None:
    """Append a small, credential-free diagnostic event to the account log."""
    if not re.fullmatch(r"[0-9a-f]{32}", str(account_id)):
        return
    safe_details = {}
    for key, value in details.items():
        text = str(value)[:1200]
        text = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[REDACTED]", text)
        text = re.sub(r"(?i)(access[_ -]?token|token|api[_ -]?key|password)(\s*[:=]\s*)[^,;\s]+", r"\1\2[REDACTED]", text)
        safe_details[str(key)[:40]] = text
    row = {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "event": str(event)[:100],
        **safe_details,
    }
    path = account_data_dir(account_id) / "cafe_automation.log"
    encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
    try:
        with _LOG_LOCK:
            if path.exists() and path.stat().st_size >= _LOG_MAX_BYTES:
                backup = path.with_suffix(".log.1")
                try:
                    backup.unlink(missing_ok=True)
                except TypeError:  # Python 3.8 compatibility
                    if backup.exists():
                        backup.unlink()
                path.replace(backup)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(encoded)
    except OSError:
        # Diagnostics must never prevent the Cafe action itself from running.
        pass


def read_cafe_log(account_id: str, max_lines: int = 300) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", str(account_id)):
        raise ValueError("카페 로그 계정이 올바르지 않습니다.")
    path = account_data_dir(account_id) / "cafe_automation.log"
    if not path.exists():
        return "아직 저장된 카페 로그가 없습니다."
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"카페 로그 파일을 읽지 못했습니다: {exc}") from exc
    return "\n".join(lines[-max(1, min(int(max_lines), 1000)):]) or "아직 저장된 카페 로그가 없습니다."


def _log(event: str, **details) -> None:
    account_id = _LOG_ACCOUNT_ID.get()
    if account_id:
        log_cafe_event(account_id, event, **details)


async def _open_page(account_id: str):
    from features.blog_publisher import _open_publish_browser

    # Match the blog account flow: login/challenge windows are visible, then
    # the actual Cafe actions reuse the saved session in headless mode.
    general = {**load_config().get("general", {}), "show_browser": False}
    credentials = load_naver_credentials(account_id)
    playwright = await async_playwright().start()
    try:
        _, page = await _open_publish_browser(playwright, general, account_id, credentials)
        return playwright, page
    except Exception:
        await playwright.stop()
        raise


async def _locate_visible(page, selectors: tuple[str, ...]):
    for frame in page.frames:
        for selector in selectors:
            locator = frame.locator(selector).first
            try:
                if await locator.is_visible(timeout=700):
                    return locator
            except Exception:
                continue
    return None


async def _locate_exact_action(page, name: str):
    """Find a visible action whose complete label matches, not a substring."""
    for frame in page.frames:
        controls = frame.locator("button, a, [role='button'], input[type='submit']")
        try:
            count = min(await controls.count(), 120)
        except Exception:
            continue
        for index in range(count):
            try:
                control = controls.nth(index)
                if not await control.is_visible():
                    continue
                label = (await control.inner_text()).strip()
                if not label:
                    label = (await control.get_attribute("aria-label") or "").strip()
                if not label and await control.get_attribute("type") == "submit":
                    label = (await control.get_attribute("value") or "").strip()
                if re.sub(r"\s+", "", label) == name:
                    return control
            except Exception:
                # Cafe's editor can contain custom or detached controls. One
                # unreadable control must not hide a later, usable action.
                continue
    return None


async def _ensure_post_body(page, word: str, force: bool = False) -> str:
    """Keep Cafe's required body nonempty; use the chain word when blank."""
    candidates = []
    selectors = (
        ".se-main-container [contenteditable='true']",
        ".se-main-container",
        "[contenteditable='true']",
        "[role='textbox']",
        "textarea[placeholder*='내용'], textarea[placeholder*='본문']",
        "textarea:not(.textarea_input)",
    )
    # Cafe initializes its body editor after the title control appears. Give
    # that late editor a short window to mount before taking a DOM inventory.
    for _ in range(12):
        if await _locate_visible(page, selectors):
            break
        await page.wait_for_timeout(250)
    for frame in page.frames:
        for selector in selectors:
            try:
                locator = frame.locator(selector)
                count = min(await locator.count(), 20)
            except Exception:
                continue
            for index in range(count):
                try:
                    field = locator.nth(index)
                    if not await field.is_visible():
                        continue
                    try:
                        value = await field.input_value()
                    except Exception:
                        value = await field.inner_text()
                    candidates.append((field, str(value).strip(), selector, re.sub(r"\?.*$", "", frame.url)))
                except Exception:
                    continue
    existing = next((value for _, value, _, _ in candidates if value), "")
    if existing and not force:
        _log("post_body_present", length=len(existing))
        return "existing"
    for field, _, selector, frame_url in candidates:
        try:
            await field.fill(word)
            try:
                await field.evaluate("el => { el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); }")
            except Exception:
                pass
            try:
                value = (await field.input_value()).strip()
            except Exception:
                value = (await field.inner_text()).strip()
            if value == word:
                try:
                    await field.press("Tab")
                except Exception:
                    pass
                _log("post_body_filled", title=word, selector=selector, frame_url=frame_url, forced=force)
                return "filled"
        except Exception:
            continue
    for frame_index, frame in enumerate(page.frames[:32]):
        try:
            state = await frame.evaluate("""() => ({
              url: location.href.split('?')[0],
              editorKeys: Object.keys(window.SmartEditor?._editors || {}),
              designMode: document.designMode,
              bodyEditable: Boolean(document.body?.isContentEditable),
              fields: [...document.querySelectorAll('textarea,[contenteditable],[role="textbox"],[data-placeholder]')].slice(0,40).map(el => ({
                tag:el.tagName, id:el.id, cls:String(el.className||'').slice(0,140), role:el.getAttribute('role'),
                placeholder:el.getAttribute('placeholder')||el.getAttribute('data-placeholder')||'',
                editable:el.getAttribute('contenteditable'), isContentEditable:el.isContentEditable,
                visible:!!(el.getClientRects().length), text:(el.innerText||el.value||'').trim().slice(0,100)
              })),
              iframes:[...document.querySelectorAll('iframe')].slice(0,24).map(el=>({id:el.id,cls:String(el.className||'').slice(0,140),title:el.title,src:(el.src||'').split('?')[0]})),
              shadowHosts:[...document.querySelectorAll('*')].filter(el=>el.shadowRoot).slice(0,8).map(el=>({tag:el.tagName,id:el.id,cls:String(el.className||'').slice(0,100),fields:[...el.shadowRoot.querySelectorAll('textarea,[contenteditable],[role="textbox"]')].slice(0,6).map(field=>({tag:field.tagName,id:field.id,cls:String(field.className||'').slice(0,100),editable:field.getAttribute('contenteditable'),text:(field.innerText||field.value||'').trim().slice(0,80)}))}))
            })""")
            _log("post_editor_frame_inventory", frame_index=frame_index, frame_url=re.sub(r"\?.*$", "", frame.url), inventory=state)
            anchors = await frame.evaluate("""() => [...document.querySelectorAll('body *')]
              .filter(el=>el.children.length===0 && /제목 양식 미준수|아래 예시 삭제/.test(el.innerText||''))
              .slice(0,3).map(el=>{const path=[];for(let node=el;node&&path.length<6;node=node.parentElement)path.push({tag:node.tagName,id:node.id,cls:String(node.className||'').slice(0,100),editable:node.getAttribute('contenteditable'),role:node.getAttribute('role')});return path})""")
            if anchors:
                _log("post_template_anchor_inventory", frame_index=frame_index, anchors=anchors)
        except Exception:
            continue
    _log("post_body_editor_not_found_continue", frame_count=len(page.frames))
    return "unknown"


async def _confirm_cafe_publish_dialog(page) -> bool:
    for frame in page.frames:
        try:
            dialogs = frame.locator("[role='dialog'], [role='alertdialog']")
            count = min(await dialogs.count(), 5)
        except Exception:
            continue
        for index in range(count):
            try:
                dialog = dialogs.nth(index)
                if not await dialog.is_visible():
                    continue
                text = re.sub(r"\s+", " ", (await dialog.inner_text()).strip())
                _log("post_dialog_seen", text=text[:500])
                if not any(phrase in text for phrase in ("등록하시겠습니까", "게시하시겠습니까", "등록할까요", "게시할까요", "등록 확인")):
                    continue
                for label in ("등록", "게시", "확인"):
                    button = dialog.get_by_role("button", name=label, exact=True).last
                    if await button.count() and await button.is_visible():
                        await button.click()
                        _log("post_dialog_confirmed", label=label)
                        return True
            except Exception:
                continue
    return False


async def _page_text(page, timeout: int = 1200) -> str:
    chunks = []
    for frame in page.frames[:5]:
        try:
            chunks.append(await frame.locator("body").inner_text(timeout=timeout))
        except Exception:
            continue
    return " ".join(chunks)


async def _run(action, account_id: str):
    log_context = _LOG_ACCOUNT_ID.set(account_id)
    playwright = None
    succeeded = False
    try:
        _log("run_started")
        playwright, page = await _open_page(account_id)
        _log("browser_attached", url=re.sub(r"\?.*$", "", page.url))
        page._cafe_dialog_messages = []
        def handle_dialog(dialog):
            page._cafe_dialog_messages.append({
                "type": getattr(dialog, "type", "unknown"),
                "message": getattr(dialog, "message", ""),
            })
            _log("browser_dialog", dialog_type=getattr(dialog, "type", "unknown"), message=getattr(dialog, "message", ""))
            asyncio.create_task(dialog.accept())
        page.on("dialog", handle_dialog)
        result = await action(page)
        succeeded = True
        _log("run_succeeded", result=result.get("message", "") if isinstance(result, dict) else "")
        return result
    except Exception as exc:
        _log("run_failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        # Keep the visible Chrome window on failures so the user can see whether
        # Naver showed a login, membership, access, or changed-page screen.
        # In CDP mode stopping Playwright only detaches; it does not close Chrome.
        try:
            browser_mode = load_config().get("general", {}).get("browser_mode", "cdp")
            if playwright is not None and (succeeded or browser_mode != "cdp"):
                try:
                    await close_naver()
                except Exception as exc:
                    _log("browser_close_failed", error=f"{type(exc).__name__}: {exc}")
            if playwright is not None:
                try:
                    await playwright.stop()
                except Exception as exc:
                    _log("playwright_detach_failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            _LOG_ACCOUNT_ID.reset(log_context)


async def _attendance(page, message: str) -> dict:
    _log("attendance_open", url=ATTENDANCE_URL.split("?", 1)[0])
    await page.goto(ATTENDANCE_URL, wait_until="domcontentloaded", timeout=45_000)
    await page.wait_for_timeout(1200)
    if "nidlogin" in page.url:
        raise RuntimeError("네이버 로그인이 만료됐어요. 계정을 다시 연결해 주세요.")
    field = await _locate_visible(page, ("#cmtinput", "textarea[name='attendancePost.content']", "textarea[placeholder*='글을 입력']"))
    if field is None:
        existing_text = await _page_text(page)
        if any(text in existing_text for text in ("이미 출석", "오늘 출석 완료", "오늘 출석을 완료", "출석하셨습니다")):
            _log("attendance_already_done")
            return {"ok": True, "already": True, "message": "오늘 출석은 이미 완료되어 건너뛰었어요."}
        raise RuntimeError("출석 입력 칸을 찾지 못했어요. 카페 출석 도장 화면을 확인해 주세요.")
    await field.fill(message)
    _log("attendance_input_filled")
    button = await _locate_visible(page, ("button:has-text('출석하기')", "a:has-text('출석하기')", "input[value='출석하기']"))
    if button is None:
        raise RuntimeError("출석하기 버튼을 찾지 못했어요. 이미 출석했거나 카페 화면이 바뀌었는지 확인해 주세요.")
    await button.click()
    _log("attendance_button_clicked")
    await page.wait_for_timeout(1200)
    body_text = await _page_text(page)
    if any(text in body_text for text in ("출석 완료", "출석하였습니다", "출석이 완료", "오늘 출석")):
        _log("attendance_confirmed")
        return {"ok": True, "message": "카페 출석을 완료했어요."}
    _log("attendance_uncertain", page_url=re.sub(r"\?.*$", "", page.url))
    return {"ok": True, "message": "출석 버튼을 눌렀어요. 열린 카페 화면에서 완료 여부를 확인해 주세요."}


async def _latest_article(page) -> dict:
    _log("board_list_open", url=WORD_CHAIN_LIST_URL)
    await page.goto(WORD_CHAIN_LIST_URL, wait_until="domcontentloaded", timeout=45_000)
    candidates = []
    for _ in range(24):
        candidates = []
        for frame in page.frames:
            try:
                links = frame.locator("a[href*='/articles/'], a[href*='ArticleRead.nhn'], a[href*='articleid=']")
                count = min(await links.count(), 200)
                _log("board_frame_scanned", frame_url=re.sub(r"\?.*$", "", frame.url), link_count=count)
            except Exception:
                continue
            for index in range(count):
                try:
                    link = links.nth(index)
                    if not await link.is_visible():
                        continue
                    href = await link.get_attribute("href") or ""
                    match = re.search(r"/articles/(\d+)", href, re.I) or re.search(r"(?:articleid|articleId)=(\d+)", href, re.I)
                    title = re.sub(r"\s+", " ", (await link.inner_text()).strip())
                    if not title:
                        title = (await link.get_attribute("title") or "").strip()
                    if title and match:
                        candidates.append((int(match.group(1)), title, href))
                except Exception:
                    # Ignore one stale/incomplete row and keep scanning the
                    # remaining article links in this Cafe frame.
                    continue
        if candidates:
            break
        await page.wait_for_timeout(500)
    if not candidates:
        if "nid.naver.com" in page.url or "nidlogin" in page.url:
            raise RuntimeError("네이버 로그인이 만료됐어요. 계정 연결을 다시 확인해 주세요.")
        body_text = re.sub(r"\s+", " ", await _page_text(page))
        detail = body_text[:180] or "페이지 본문이 비어 있어요."
        _log("board_list_empty", page_url=re.sub(r"\?.*$", "", page.url), body=detail)
        raise RuntimeError(f"끝말잇기 게시판에서 글 목록을 읽지 못했어요. 열린 주소: {page.url} / 화면 내용: {detail}")
    _, title, href = max(candidates, key=lambda row: row[0])
    _log("latest_article_found", title=title, article_id=max(candidates, key=lambda row: row[0])[0], candidate_count=len(candidates))
    url = href if href.startswith("http") else "https://cafe.naver.com" + href
    return {"ok": True, "title": title, "url": url}


async def _article_title_exists(page, title: str) -> bool:
    """Confirm a submitted title from the board itself when Naver keeps the editor URL."""
    expected = re.sub(r"\s+", " ", title).strip()
    await page.goto(WORD_CHAIN_LIST_URL, wait_until="domcontentloaded", timeout=45_000)
    _log("publish_fallback_list_check_started", title=title)
    for _ in range(12):
        for frame in page.frames:
            try:
                links = frame.locator("a[href*='/articles/'], a[href*='ArticleRead.nhn'], a[href*='articleid=']")
                count = min(await links.count(), 200)
            except Exception:
                continue
            for index in range(count):
                try:
                    link = links.nth(index)
                    if not await link.is_visible():
                        continue
                    href = await link.get_attribute("href") or ""
                    if not (
                        re.search(r"/articles/\d+", href, re.I)
                        or re.search(r"(?:articleid|articleId)=\d+", href, re.I)
                    ):
                        continue
                    found = re.sub(r"\s+", " ", (await link.inner_text()).strip())
                    if not found:
                        found = re.sub(r"\s+", " ", (await link.get_attribute("title") or "").strip())
                    if found == expected:
                        _log("publish_confirmed_from_list", title=title, href=href)
                        return True
                except Exception:
                    continue
        await page.wait_for_timeout(500)
    _log("publish_not_found_in_list", title=title)
    return False


async def _post_word(page, title: str) -> dict:
    _log("post_open_editor", title=title, url=WORD_CHAIN_WRITE_URL)
    await page.goto(WORD_CHAIN_WRITE_URL, wait_until="domcontentloaded", timeout=45_000)
    if "nidlogin" in page.url:
        raise RuntimeError("네이버 로그인이 만료됐어요. 계정을 다시 연결해 주세요.")
    title_field = None
    for _ in range(12):
        title_field = await _locate_visible(page, ("textarea.textarea_input", "textarea[placeholder*='제목']", "input[placeholder*='제목']"))
        if title_field is not None:
            break
        await page.wait_for_timeout(500)
    if title_field is None:
        page_text = re.sub(r"\s+", " ", await _page_text(page))[:180]
        raise RuntimeError(f"끝말잇기 글쓰기 제목 칸을 찾지 못했어요. 현재 주소: {page.url} / 화면 내용: {page_text}")
    # Use fill for Hangul: Playwright's key-by-key typing may not synthesize
    # Korean IME input and can leave this textarea empty.
    await title_field.fill(title)
    try:
        await title_field.evaluate("el => { el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); }")
    except Exception:
        pass
    try:
        entered_title = (await title_field.input_value()).strip()
    except Exception as exc:
        raise RuntimeError(f"제목 입력 여부를 확인하지 못해 등록을 중단했어요. ({exc})") from exc
    if entered_title != title:
        _log("post_title_mismatch", expected=title, actual=entered_title)
        raise RuntimeError(f"제목 입력이 확인되지 않아 등록하지 않았어요. 입력값: {entered_title!r}")
    _log("post_title_verified", title=title)
    await _ensure_post_body(page, title)
    entered_title = (await title_field.input_value()).strip()
    if entered_title != title:
        _log("post_title_lost_before_register", expected=title, actual=entered_title)
        await title_field.fill(title)
        entered_title = (await title_field.input_value()).strip()
        if entered_title != title:
            raise RuntimeError(f"등록 직전 제목값이 사라졌어요. 입력값: {entered_title!r}")
        _log("post_title_restored_before_register", title=title)
    register = await _locate_exact_action(page, "등록")
    if register is None:
        _log("post_register_button_missing", title=title, page_url=re.sub(r"\?.*$", "", page.url))
        raise RuntimeError("‘등록’ 버튼을 찾지 못했어요. 임시등록 버튼은 누르지 않았고, 글도 등록하지 않았습니다.")
    dialog_messages = getattr(page, "_cafe_dialog_messages", [])
    dialog_cursor = len(dialog_messages)
    await register.click()
    _log("post_register_clicked", title=title)
    new_dialogs = dialog_messages[dialog_cursor:]
    missing_title_dialog = next(
        (item for item in new_dialogs if item.get("type") == "alert" and "제목을 입력" in item.get("message", "")),
        None,
    )
    missing_body_dialog = next(
        (item for item in new_dialogs if item.get("type") == "alert" and any(phrase in item.get("message", "") for phrase in ("내용을 입력", "본문을 입력"))),
        None,
    )
    if missing_body_dialog:
        _log("post_body_rejected_by_editor", title=title, message=missing_body_dialog["message"], retry="refill_body")
        body_state = await _ensure_post_body(page, title, force=True)
        if body_state not in ("filled", "existing"):
            raise RuntimeError("등록 버튼을 눌렀지만 카페가 본문을 비었다고 판단했고, 본문 재입력 영역도 찾지 못했어요. 글은 등록되지 않았습니다.")
        await title_field.fill(title)
        retry_title = (await title_field.input_value()).strip()
        if retry_title != title:
            raise RuntimeError("본문 재입력 후 제목이 사라져 등록을 재시도하지 않았어요.")
        dialog_cursor = len(dialog_messages)
        await register.click()
        _log("post_register_retried_after_body_rejection", title=title, body_state=body_state)
        retry_dialogs = dialog_messages[dialog_cursor:]
        retry_body_dialog = next(
            (item for item in retry_dialogs if item.get("type") == "alert" and any(phrase in item.get("message", "") for phrase in ("내용을 입력", "본문을 입력"))),
            None,
        )
        if retry_body_dialog:
            _log("post_body_retry_rejected", title=title, message=retry_body_dialog["message"])
            raise RuntimeError("카페가 본문을 계속 비었다고 판단해 등록을 거부했어요. 게시판에 등록되지 않았습니다.")
    if missing_title_dialog:
        _log("post_title_rejected_by_editor", title=title, message=missing_title_dialog["message"], retry="fill_retry")
        await title_field.fill(title)
        try:
            await title_field.evaluate("el => { el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); }")
        except Exception:
            pass
        retried_title = (await title_field.input_value()).strip()
        if retried_title != title:
            raise RuntimeError(f"카페 작성기가 제목을 입력받지 못했어요. 입력값: {retried_title!r}")
        dialog_cursor = len(dialog_messages)
        await register.click()
        _log("post_register_retried_after_title_rejection", title=title)
        retry_dialogs = dialog_messages[dialog_cursor:]
        if any(item.get("type") == "alert" and "제목을 입력" in item.get("message", "") for item in retry_dialogs):
            raise RuntimeError("카페 작성기가 제목 입력을 계속 거부했어요. 글은 등록되지 않았습니다.")
    success_markers = (
        "등록되었습니다", "등록했습니다", "게시글이 등록", "게시물이 등록",
        "글이 등록", "등록이 완료", "게시가 완료", "게시되었습니다",
    )
    body_text = ""
    published_url = False
    confirmed = False
    dialog_confirmed = False
    validation_markers = (
        "본문을 입력해 주세요", "내용을 입력해 주세요", "제목을 입력해 주세요",
        "말머리를 선택해 주세요", "등록할 수 없습니다", "게시할 수 없습니다",
        "글쓰기 권한이 없습니다", "카페 회원만 작성",
    )
    for _ in range(24):
        await page.wait_for_timeout(500)
        body_text = await _page_text(page, timeout=400)
        if not dialog_confirmed:
            dialog_confirmed = await _confirm_cafe_publish_dialog(page)
        published_url = bool(
            re.search(r"/articles/\d+(?:[/?#]|$)", page.url, re.I)
            or re.search(r"[?&]articleid=\d+", page.url, re.I)
        )
        if published_url or any(marker in body_text for marker in success_markers):
            confirmed = True
            _log("post_confirmed_immediately", title=title, page_url=re.sub(r"\?.*$", "", page.url), url_match=published_url)
            break
        validation = next((marker for marker in validation_markers if marker in body_text), "")
        if validation:
            _log("post_validation_failed", marker=validation, body=body_text[:1000])
            raise RuntimeError(f"네이버 카페 등록 검증에서 멈췄어요: {validation}")
    if not confirmed:
        _log("post_editor_after_click", page_url=re.sub(r"\?.*$", "", page.url), body=body_text[:1000], dialog_confirmed=dialog_confirmed)
        confirmed = await _article_title_exists(page, title)
    if not confirmed:
        _log("post_confirmation_failed", title=title, page_url=re.sub(r"\?.*$", "", page.url), body=body_text[:180])
        raise RuntimeError(f"실제 게시글 등록을 확인하지 못했어요. 임시저장과 구분하기 위해 카페 게시판을 확인해 주세요. 현재 주소: {page.url}")
    _log("post_succeeded", title=title)
    return {"ok": True, "title": title, "url": page.url, "message": f"‘{title}’ 글을 등록했어요."}


async def _post_chain(page, latest: str, words: list[str]) -> dict:
    previous = latest[-1]
    completed = []
    for index, word in enumerate(words, 1):
        _log("chain_post_started", index=index, total=len(words), word=word)
        if word[0] != previous:
            raise ValueError(f"{index}번째 단어가 앞 단어와 이어지지 않아요.")
        try:
            await _post_word(page, word)
        except Exception as exc:
            _log("chain_post_failed", index=index, word=word, completed=completed, error=f"{type(exc).__name__}: {exc}")
            if completed:
                raise RuntimeError(
                    f"{', '.join(completed)} 글은 등록됐고 {index}번째 ‘{word}’에서 멈췄어요. "
                    f"중복 등록을 막기 위해 게시판을 확인한 뒤 이어서 진행해 주세요. ({exc})"
                ) from exc
            raise
        completed.append(word)
        _log("chain_post_completed", index=index, word=word)
        if index < len(words):
            await page.wait_for_timeout(1200)
        previous = word[-1]
    return {"ok": True, "posts": words, "message": f"끝말잇기 글 {len(words)}개를 차례대로 등록했어요."}


_FALLBACK_CHAINS = {
    "소": ["소금", "금속", "속옷"],
    "담": ["담요", "요리", "리본"],
    "기": ["기차", "차표", "표범"],
    "일": ["일기", "기차", "차표"],
    "사": ["사과", "과자", "자동차"],
    "과": ["과자", "자동차", "차표"],
    "차": ["차표", "표범", "범죄"],
    "표": ["표범", "범죄", "죄수"],
    "범": ["범죄", "죄수", "수박"],
    "수": ["수박", "박수", "수업"],
    "업": ["업무", "무지개", "개나리"],
    "리": ["리본", "본능", "능력"],
    "력": ["역사", "사과", "과자"],
    "음": ["음악", "악기", "기차"],
    "악": ["악기", "기차", "차표"],
    "옷": ["옷장", "장미", "미소"],
    "장": ["장미", "미소", "소금"],
    "미": ["미소", "소금", "금속"],
    "은": ["은행", "행운", "운전"],
    "행": ["행운", "운전", "전기"],
    "운": ["운전", "전기", "기차"],
    "전": ["전기", "기차", "차표"],
    "화": ["화분", "분필", "필통"],
    "통": ["통장", "장미", "미소"],
    "품": ["품질", "질서", "서랍"],
    "봄": ["봄날", "날씨", "씨앗"],
    "정": ["정리", "리본", "본능"],
    "벌": ["벌금", "금속", "속옷"],
    "갑": ["갑옷", "옷장", "장미"],
    "집": ["집게", "게장", "장미"],
    "말": ["말벌", "벌금", "금속"],
}


def _fallback_chain(ending: str) -> list[str] | None:
    words = _FALLBACK_CHAINS.get(ending)
    if words and len(words) == 3 and all(words[i][0] == (ending if i == 0 else words[i - 1][-1]) for i in range(3)):
        return list(words)
    return None


async def _generate_chain(ending: str, api_key: str, access_token: str | None, model: str) -> list[str]:
    from core.llm import create_openai_client

    client = create_openai_client(api_key, access_token=access_token)
    generation_error = ""
    for attempt in range(2):
        try:
            response = await asyncio.to_thread(
                client.chat.completions.create,
                model=model,
                temperature=0.25,
                max_tokens=120,
                messages=[
                    {"role": "system", "content": "한국어 끝말잇기 단어를 고릅니다. 반드시 실제로 쓰이는 자연스러운 명사 세 개만 반환합니다."},
                    {"role": "user", "content": (
                        f"시작 음절은 '{ending}'입니다. 네이버 카페 끝말잇기 글 제목으로 쓸 한국어 명사 3개를 만드세요. "
                        "첫 단어는 시작 음절로 시작하고, 다음 단어는 바로 앞 단어의 마지막 음절로 시작해야 합니다. "
                        "각 단어는 한글 명사 하나로, 띄어쓰기 없이 2~6글자여야 합니다. "
                        "유명 인명, 고유명사, 조어를 피하고 사전에 실릴 만한 흔한 단어를 고르세요. "
                        "쉼표로 구분한 단어 세 개만 반환하세요."
                    )},
                ],
            )
        except Exception as exc:
            generation_error = f"{type(exc).__name__}: {exc}"
            _log("chain_generation_error", ending=ending, attempt=attempt + 1, error=generation_error)
            break
        raw = (response.choices[0].message.content or "").strip()
        _log("chain_generation_response", ending=ending, attempt=attempt + 1, raw=raw)
        words = [re.sub(r"[^가-힣]", "", word) for word in re.split(r"[,，\n]+", raw)]
        words = [word for word in words if word]
        previous = ending
        valid = len(words) == 3
        for word in words:
            valid = valid and 2 <= len(word) <= 6 and word[0] == previous
            if not word:
                valid = False
                break
            previous = word[-1]
        if valid:
            return words
        _log("chain_generation_rejected", ending=ending, attempt=attempt + 1, words=words)
    fallback = _fallback_chain(ending)
    if fallback:
        _log("chain_generation_fallback", ending=ending, words=fallback, model_error=generation_error)
        return fallback
    _log("chain_generation_failed", ending=ending, model_error=generation_error)
    raise RuntimeError(f"‘{ending}’로 시작하는 끝말잇기 단어 3개를 만들지 못했어요. 로그에서 AI 응답을 확인해 주세요.")


async def _daily_cafe(page, message: str, api_key: str, access_token: str | None, model: str) -> dict:
    _log("daily_started", model=model)
    latest = await _latest_article(page)
    endings = re.findall(r"[가-힣]", latest["title"])
    if not endings:
        raise RuntimeError("최신 글 제목에서 끝말잇기 음절을 찾지 못했어요.")
    ending = endings[-1]
    words = await _generate_chain(ending, api_key, access_token, model)
    _log("chain_words_generated", ending=ending, words=words)
    attendance = await _attendance(page, message)
    _log("daily_attendance_finished", already=attendance.get("already", False))
    posted = await _post_chain(page, ending, words)
    _log("daily_finished", latest_title=latest["title"], posts=posted.get("posts", []))
    return {
        "ok": True,
        "attendance": attendance,
        "latest_title": latest["title"],
        "words": words,
        "posts": posted.get("posts", []),
        "message": f"출석 처리 후 ‘{latest['title']}’에 이어 {', '.join(words)} 글 3개를 등록했어요.",
    }


def attend(account_id: str, message: str = "출석합니다.") -> dict:
    message = str(message).strip()[:100]
    if not message:
        raise ValueError("출석 문구를 입력해 주세요.")
    return asyncio.run(_run(lambda page: _attendance(page, message), account_id))


def latest_article(account_id: str) -> dict:
    return asyncio.run(_run(_latest_article, account_id))


def post_word(account_id: str, title: str) -> dict:
    title = re.sub(r"\s+", " ", str(title)).strip()[:80]
    if not title:
        raise ValueError("등록할 끝말잇기 단어를 입력해 주세요.")
    return asyncio.run(_run(lambda page: _post_word(page, title), account_id))


def post_chain(account_id: str, latest: str, words: list[str]) -> dict:
    latest = re.sub(r"\s+", " ", str(latest)).strip()[:80]
    words = [re.sub(r"\s+", " ", str(word)).strip()[:80] for word in words]
    if not latest or len(words) != 3 or any(not word for word in words):
        raise ValueError("최신 단어와 끝말잇기 단어 3개를 입력해 주세요.")
    previous = latest[-1]
    for index, word in enumerate(words, 1):
        if word[0] != previous:
            raise ValueError(f"{index}번째 단어가 앞 단어와 이어지지 않아요.")
        previous = word[-1]
    return asyncio.run(_run(lambda page: _post_chain(page, latest, words), account_id))


def run_daily(account_id: str, message: str, api_key: str, access_token: str | None, model: str) -> dict:
    message = str(message).strip()[:100]
    if not message:
        raise ValueError("출석 문구를 입력해 주세요.")
    return asyncio.run(_run(lambda page: _daily_cafe(page, message, api_key, access_token, model), account_id))
