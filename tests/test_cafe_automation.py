import asyncio
import unittest
import tempfile
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from features.cafe_automation import _generate_chain, _latest_article, _post_chain, _post_word, _run, log_cafe_event, read_cafe_log


class FakeControl:
    def __init__(self, label, page=None, broken=False):
        self.label = label
        self.page = page
        self.broken = broken

    async def is_visible(self, **_kwargs):
        if self.broken:
            raise RuntimeError("detached editor control")
        return True

    async def inner_text(self):
        if self.broken:
            raise RuntimeError("detached editor control")
        return self.label

    async def get_attribute(self, _name):
        return None

    async def click(self):
        if self.label != "등록":
            raise AssertionError("temporary save must never be clicked")
        if self.page.title_rejections_remaining:
            self.page.title_rejections_remaining -= 1
            self.page._cafe_dialog_messages.append({"type": "alert", "message": "제목을 입력하세요."})
            return
        if self.page.body_rejection_message:
            self.page._cafe_dialog_messages.append({"type": "alert", "message": self.page.body_rejection_message})
            self.page.body_rejection_message = ""
            return
        self.page.posts.append(self.page.title)
        if self.page.keep_editor_url:
            href = f"/f-e/cafes/30136794/articles/{len(self.page.posts)}"
            self.page.article_links.append(FakeArticleLink(href, self.page.title))
            self.page.body_posts.append(self.page.body)
            self.page.title = ""
            return
        self.page.body_posts.append(self.page.body)
        self.page.url = f"https://cafe.naver.com/f-e/cafes/30136794/articles/{len(self.page.posts)}"

    async def evaluate(self, *_args):
        pass


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector
        self.first = self

    async def is_visible(self, **_kwargs):
        return self.selector.startswith("textarea")

    async def fill(self, value):
        self.page.title = value

    async def evaluate(self, *_args):
        pass

    async def input_value(self):
        return self.page.title

    async def count(self):
        if self.selector.startswith("button,"):
            return len(self.page.controls)
        return 0

    def nth(self, index):
        return self.page.controls[index]

    async def inner_text(self, **_kwargs):
        return ""


class FakeFrame:
    def __init__(self, page):
        self.page = page
        self.url = "https://cafe.naver.com/f-e/cafes/30136794/menus/59"

    def locator(self, selector):
        if "a[href*='/articles/']" in selector:
            return FakeArticleLinks(self.page.article_links)
        if "contenteditable" in selector or selector == ".se-main-container":
            if self.page.editor_missing:
                return EmptyLocator()
            return FakeBodyLocator(self.page)
        return FakeLocator(self.page, selector)


class EmptyLocator:
    @property
    def first(self):
        return self

    async def count(self):
        return 0


class FakeBodyLocator:
    def __init__(self, page):
        self.page = page
        self.first = self

    async def count(self):
        return 1

    def nth(self, _index):
        return self

    async def is_visible(self):
        return True

    async def input_value(self):
        return self.page.body

    async def inner_text(self):
        return self.page.body

    async def fill(self, value):
        self.page.body = value

    async def evaluate(self, *_args):
        pass

    async def press(self, _key):
        pass


class FakeArticleLink:
    def __init__(self, href, title="", broken=False):
        self.href = href
        self.title = title
        self.broken = broken

    async def is_visible(self):
        if self.broken:
            raise RuntimeError("stale list row")
        return True

    async def get_attribute(self, name):
        return self.href if name == "href" else None

    async def inner_text(self):
        return self.title


class FakeArticleLinks:
    def __init__(self, links):
        self.links = links

    async def count(self):
        return len(self.links)

    def nth(self, index):
        return self.links[index]


class FakePage:
    def __init__(self):
        self.posts = []
        self.body_posts = []
        self.title = ""
        self.body = ""
        self.article_links = []
        self.keep_editor_url = False
        self.editor_missing = False
        self.title_rejections_remaining = 0
        self.body_rejection_message = ""
        self._cafe_dialog_messages = []
        self.url = ""
        # Include both a broken editor control and the dangerous substring
        # case from the UI: '임시등록' must not count as '등록'.
        self.controls = [
            FakeControl("임시등록", self),
            FakeControl("broken", self, broken=True),
            FakeControl("등록", self),
        ]
        self.frames = [FakeFrame(self)]

    async def goto(self, url, **_kwargs):
        self.url = url
        self.title = ""
        self.body = ""

    async def wait_for_timeout(self, _milliseconds):
        pass


class CafePostingFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_model_chain_uses_curated_fallback(self):
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="소금, 바다, 속옷"))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(return_value=response))))
        with patch("core.llm.create_openai_client", return_value=client):
            words = await _generate_chain("소", "test-key", None, "gpt-4o-mini")

        self.assertEqual(words, ["소금", "금속", "속옷"])

    async def test_run_logs_start_and_success_without_keyword_collision(self):
        class FakePage:
            url = "https://cafe.naver.com/"

            def on(self, *_args):
                pass

        class FakePlaywright:
            stop = AsyncMock()

        page = FakePage()
        playwright = FakePlaywright()
        with tempfile.TemporaryDirectory() as folder:
            with patch("features.cafe_automation.account_data_dir", return_value=Path(folder)), \
                 patch("features.cafe_automation._open_page", new=AsyncMock(return_value=(playwright, page))), \
                 patch("features.cafe_automation.load_config", return_value={"general": {"browser_mode": "cdp"}}), \
                 patch("features.cafe_automation.close_naver", new=AsyncMock()):
                result = await _run(lambda _page: asyncio.sleep(0, result={"ok": True}), "a" * 32)
                text = read_cafe_log("a" * 32)

        self.assertTrue(result["ok"])
        self.assertIn('"event":"run_started"', text)
        self.assertIn('"event":"run_succeeded"', text)

    def test_diagnostics_are_persisted_and_secrets_are_redacted(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("features.cafe_automation.account_data_dir", return_value=Path(folder)):
                log_cafe_event("a" * 32, "run_failed", error="token=supersecret sk-abcdefghijklmnop")
                text = read_cafe_log("a" * 32)

        self.assertIn("run_failed", text)
        self.assertIn("[REDACTED]", text)
        self.assertNotIn("supersecret", text)
        self.assertNotIn("sk-abcdefghijklmnop", text)

    async def test_post_is_confirmed_from_board_when_editor_url_does_not_change(self):
        page = FakePage()
        page.keep_editor_url = True

        result = await _post_word(page, "면사포")

        self.assertEqual(page.posts, ["면사포"])
        self.assertEqual(result["title"], "면사포")
        self.assertTrue(page.url.endswith("menus/59?viewType=L"))

    async def test_missing_body_editor_selector_does_not_block_publish_attempt(self):
        page = FakePage()
        page.editor_missing = True

        result = await _post_word(page, "면사포")

        self.assertEqual(page.posts, ["면사포"])
        self.assertEqual(result["title"], "면사포")

    async def test_native_title_validation_retries_title_fill(self):
        page = FakePage()
        page.title_rejections_remaining = 1

        result = await _post_word(page, "면사포")

        self.assertEqual(page.posts, ["면사포"])
        self.assertEqual(result["title"], "면사포")

    async def test_native_body_validation_reports_that_register_was_clicked(self):
        page = FakePage()
        page.body_rejection_message = "내용을 입력하세요."

        result = await _post_word(page, "면사포")

        self.assertEqual(page.posts, ["면사포"])
        self.assertEqual(result["title"], "면사포")

    async def test_latest_article_skips_a_stale_row_and_reads_next_article(self):
        page = FakePage()
        page.article_links = [
            FakeArticleLink("/f-e/cafes/30136794/articles/5224", broken=True),
            FakeArticleLink("/f-e/cafes/30136794/articles/5223", "사면"),
        ]

        result = await _latest_article(page)

        self.assertEqual(result["title"], "사면")
        self.assertTrue(result["url"].endswith("/articles/5223"))

    async def test_chain_publishes_three_posts_and_skips_temporary_save(self):
        page = FakePage()

        result = await _post_chain(page, "사면", ["면사포", "포도", "도자기"])

        self.assertEqual(page.posts, ["면사포", "포도", "도자기"])
        self.assertEqual(page.body_posts, ["면사포", "포도", "도자기"])
        self.assertEqual(result["posts"], page.posts)
        self.assertIn("articles/3", page.url)


if __name__ == "__main__":
    unittest.main()
