"""대상 탐색: 네이버 블로그 '주제별 글 보기' 목록에서 작성자 수집"""
from __future__ import annotations
from playwright.async_api import Page

from core.storage import Store
from core.utils import BLOGID_RE, POST_RE, human_wait

SEL_LIST_ITEM = "div.list_post_article"   # 목록의 글 한 개 (상단 hot_topic 제외)


def _extract_blog_id(url: str) -> str | None:
    m = POST_RE.search(url)
    if m:
        return m.group(1)
    m = BLOGID_RE.search(url)
    return m.group(1) if m else None


async def collect_page(page: Page, n: int, store: Store, cfg_discover: dict) -> list[str] | None:
    """n페이지의 새 블로그를 store 에 추가하고 새 ID 목록 반환. 목록을 못 찾으면 None"""
    try:
        await page.goto(cfg_discover["theme_url"].format(page=n), wait_until="domcontentloaded")
        await page.wait_for_selector(SEL_LIST_ITEM, timeout=15_000)
        await human_wait(1000, 2000)
    except Exception:
        print(f"[{n}페이지] 글 목록을 찾지 못함")
        return None

    items = await page.eval_on_selector_all(
        SEL_LIST_ITEM,
        """els => els.map(el => {
            const urls = [...el.querySelectorAll('a[href]')].map(a => a.href);
            el.querySelectorAll('[post-url]').forEach(e => urls.push(e.getAttribute('post-url')));
            return urls;
        })""",
    )

    new_ids = []
    for urls in items:
        post_url = next((u for u in urls if POST_RE.search(u)), "")
        blog_id = next((bid for u in urls if (bid := _extract_blog_id(u))), None)
        if blog_id and store.add(blog_id, post_url):
            new_ids.append(blog_id)

    store.save()
    print(f"\n📄 [{n}페이지] 글 {len(items)}개 중 새 블로그 {len(new_ids)}개")
    return new_ids
