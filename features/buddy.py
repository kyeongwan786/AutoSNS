"""자동 서이추: 서로이웃 신청 페이지에서 서로이웃 선택 + 메시지 입력 + 확인"""
from __future__ import annotations
import random
import re

from playwright.async_api import Page

from core.utils import DialogCatcher, assert_logged_in, human_wait, type_like_human

NAME = "buddy"
LABEL = "🤝 서이추"

ADD_URL = "https://m.blog.naver.com/BuddyAddForm.naver?blogId={blog_id}"

SEL_BOTH_RADIO = "#bothBuddyRadio"
SEL_BOTH_LABEL = "label[for='bothBuddyRadio']"
SEL_MESSAGE = "textarea"
SEL_CONFIRM = "button:has-text('확인'), a:has-text('확인')"

LIMIT_PATTERN = re.compile(r"더 이상 이웃|추가할 수 없|이웃수를 제한|1일\s*동안|(하루|일일|오늘).*(초과|제한|더 이상)|더 이상 신청")
SKIP_PATTERN = re.compile(r"이미|본인|자신|받지 않|불가|없는 블로그")

_last_message: str | None = None


def _pick_message(messages: list[str]) -> str:
    global _last_message
    candidates = [m for m in messages if m != _last_message] or messages
    _last_message = random.choice(candidates)
    return _last_message


def _classify(text: str, default: str) -> str:
    if LIMIT_PATTERN.search(text):
        return "limit"
    if SKIP_PATTERN.search(text):
        return "skipped"
    return default


async def run(page: Page, blog_id: str, cfg: dict, dialogs: DialogCatcher) -> tuple[str, str]:
    """반환: (status, detail)  status = done | skipped | global_limit | error"""
    dialogs.clear()
    await page.goto(ADD_URL.format(blog_id=blog_id), wait_until="domcontentloaded")
    await human_wait(1500, 3000)
    assert_logged_in(page)

    if dialogs.last:  # 이미 이웃, 본인 블로그 등
        status = _classify(dialogs.last, "skipped")
        return ("global_limit" if status == "limit" else status), dialogs.last

    radio = page.locator(SEL_BOTH_RADIO)
    if await radio.count() == 0:
        return "skipped", "서로이웃 옵션 없음"
    if await radio.is_disabled():
        return "skipped", "서로이웃 신청을 받지 않는 블로그"

    try:
        await radio.check(timeout=3_000)
    except Exception:
        await page.locator(SEL_BOTH_LABEL).click()
    await human_wait()

    message = _pick_message(cfg["messages"])
    box = page.locator(SEL_MESSAGE).first
    await box.fill("")
    await type_like_human(box, message)
    await human_wait(1000, 2500)

    await page.locator(SEL_CONFIRM).first.click()
    await human_wait(2000, 3500)

    if dialogs.last:
        status = _classify(dialogs.last, "done")
        if status == "limit":
            return "global_limit", dialogs.last
        return status, dialogs.last if status != "done" else message
    return "done", message
