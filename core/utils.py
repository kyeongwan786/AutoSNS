from __future__ import annotations
import asyncio
import random
import re

from playwright.async_api import Page

# blog.naver.com/블로그ID/글번호
POST_RE = re.compile(r"blog\.naver\.com/([A-Za-z0-9_-]+)/(\d+)")
BLOGID_RE = re.compile(r"[?&]blogId=([A-Za-z0-9_-]+)")


# 동작 속도 배율 (1.0 = 기본, 0.15 = 대기 시간 15%로 단축). 테스트 모드에서 줄임
_speed = 1.0


def set_speed(factor: float) -> None:
    global _speed
    _speed = factor


async def human_wait(lo_ms: int = 800, hi_ms: int = 2000) -> None:
    await asyncio.sleep(random.randint(lo_ms, hi_ms) / 1000 * _speed)


def typing_delay() -> int:
    """(예전 코드 호환용) 글자 사이 간격 (ms)"""
    return max(5, int(random.randint(40, 110) * _speed))


async def human_type(page: Page, text: str) -> None:
    """현재 포커스된 입력창에 사람처럼 한 글자씩 입력.
    글자마다 간격이 다르고, 띄어쓰기/문장부호 뒤엔 살짝 더 쉬고, 가끔 생각하듯 멈춤."""
    for ch in text:
        await page.keyboard.type(ch)
        ms = random.uniform(45, 170)                 # 기본 글자 간격
        if ch in " ,.!?~":
            ms += random.uniform(40, 220)            # 띄어쓰기, 문장부호 뒤
        if random.random() < 0.06:
            ms += random.uniform(300, 1100)          # 가끔 멈칫
        await asyncio.sleep(ms / 1000 * _speed)


async def type_like_human(locator, text: str) -> None:
    await locator.click()
    await human_wait(200, 600)
    await human_type(locator.page, text)


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
