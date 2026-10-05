from __future__ import annotations
import asyncio
import random
import re

from playwright.async_api import Page

# blog.naver.com/블로그ID/글번호
POST_RE = re.compile(r"blog\.naver\.com/([A-Za-z0-9_-]+)/(\d+)")
BLOGID_RE = re.compile(r"[?&]blogId=([A-Za-z0-9_-]+)")


# 동작 속도 배율 (1.0 = 기본, 0.3 = 대기 시간 30%로 단축). 테스트 모드에서 줄임
_speed = 1.0


def set_speed(factor: float) -> None:
    global _speed
    _speed = factor


async def human_wait(lo_ms: int = 800, hi_ms: int = 2000) -> None:
    await asyncio.sleep(random.randint(lo_ms, hi_ms) / 1000 * _speed)


def typing_delay() -> int:
    """글자 사이 간격 (ms)"""
    return max(5, int(random.randint(40, 110) * _speed))


async def type_like_human(locator, text: str) -> None:
    await locator.click()
    await locator.press_sequentially(text, delay=typing_delay())


class LoggedOutError(Exception):
    """진행 중 로그아웃(로그인 페이지로 튕김) 감지"""


def assert_logged_in(page: Page) -> None:
    if "nidlogin" in page.url or "nid.naver.com/nidlogin" in page.url:
        raise LoggedOutError("로그인 페이지로 이동됨")


class DialogCatcher:
    """페이지에 뜨는 alert/confirm 창을 자동으로 확인하고 문구를 모아둠"""

    def __init__(self, page: Page):
        self.messages: list[str] = []
        page.on("dialog", self._on_dialog)

    async def _on_dialog(self, dialog) -> None:
        self.messages.append(dialog.message)
        await dialog.accept()

    def clear(self) -> None:
        self.messages.clear()

    @property
    def last(self) -> str | None:
        return self.messages[-1] if self.messages else None
