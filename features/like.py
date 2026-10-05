"""자동 공감: open_post 로 이미 열린 글에서 공감 버튼 누르기"""
from __future__ import annotations
from playwright.async_api import Page

from core.utils import human_wait

NAME = "like"
LABEL = "❤️ 공감"

SEL_LIKE_BTN = ".u_likeit_list_btn, ._reactionModule a, a.btn_like"


async def run(page: Page) -> tuple[str, str]:
    """반환: (status, detail)  status = done | skipped | error"""
    btn = page.locator(SEL_LIKE_BTN).first
    if await btn.count() == 0:
        return "skipped", "공감 버튼 없음"

    await btn.scroll_into_view_if_needed()
    await human_wait(500, 1200)

    pressed = await btn.get_attribute("aria-pressed")
    classes = (await btn.get_attribute("class") or "").split()
    if pressed == "true" or "on" in classes or "_on" in classes:
        return "skipped", "이미 공감함"

    await btn.click()
    await human_wait(800, 1500)
    return "done", ""
