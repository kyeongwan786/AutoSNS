"""블로그 글 열기 + 제목/본문 읽기 (공감, 댓글이 같이 씀)"""
from __future__ import annotations
import random
from dataclasses import dataclass

from playwright.async_api import Page

from core.utils import POST_RE, assert_logged_in, human_wait

POST_URL_M = "https://m.blog.naver.com/{blog_id}/{log_no}"

SEL_POST_TITLE = ".se-title-text, .se_title, .tit_h3"
SEL_POST_BODY = ".se-main-container, #viewTypeSelector, .post_ct"


@dataclass
class Post:
    blog_id: str
    log_no: str
    title: str
    body: str


async def _first_text(page: Page, selector: str) -> str:
    loc = page.locator(selector).first
    try:
        if await loc.count():
            return (await loc.inner_text(timeout=3_000)).strip()
    except Exception:
        pass
    return ""


async def open_post(page: Page, blog_id: str, post_url: str) -> Post | None:
    m = POST_RE.search(post_url or "")
    if not m:
        return None
    log_no = m.group(2)

    await page.goto(POST_URL_M.format(blog_id=blog_id, log_no=log_no), wait_until="domcontentloaded")
    await human_wait(2000, 4000)
    assert_logged_in(page)
    # 아래까지 내려야 공감 버튼 등이 로딩되는 경우가 있어서 스크롤
    for _ in range(random.randint(3, 5)):
        await page.mouse.wheel(0, random.randint(500, 1000))
        await human_wait(700, 1600)

    return Post(
        blog_id=blog_id,
        log_no=log_no,
        title=await _first_text(page, SEL_POST_TITLE),
        body=await _first_text(page, SEL_POST_BODY),
    )
