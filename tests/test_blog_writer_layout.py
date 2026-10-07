import unittest

from core.blog_writer import _polish_editor_blocks


class BlogWriterLayoutTests(unittest.TestCase):
    def test_repeated_quote_stays_in_article_only_once(self):
        blocks = [
            {"type": "paragraph", "text": "입장은 무료다. 체험은 유료다."},
            {"type": "quote", "text": "체험은 유료다.", "style": "quotation_line"},
        ]

        result = _polish_editor_blocks(blocks, {"length": "medium", "add_subheading": False})

        self.assertEqual([block["text"] for block in result if block["type"] == "paragraph"], ["입장은 무료다."])
        self.assertEqual([block["text"] for block in result if block["type"] == "quote"], ["체험은 유료다."])

    def test_missing_quote_promotes_specific_fact_and_limits_decoration(self):
        blocks = [
            {"type": "heading", "text": "기본 정보"},
            {"type": "divider"},
            {"type": "paragraph", "text": "축제는 10월 7일부터 11일까지 열린다. 입장은 무료지만 체험은 유료다."},
            {"type": "heading", "text": "먹거리"},
            {"type": "paragraph", "text": "현장에서 한우를 직접 구워 먹을 수 있다."},
            {"type": "divider"},
            {"type": "heading", "text": "공연"},
            {"type": "paragraph", "text": "공연은 저녁에 열린다."},
        ]

        result = _polish_editor_blocks(blocks, {"length": "long", "add_subheading": True})

        self.assertEqual([block["text"] for block in result if block["type"] == "quote"], ["입장은 무료지만 체험은 유료다."])
        self.assertLessEqual(sum(block["type"] == "heading" for block in result), 3)
        self.assertLessEqual(sum(block["type"] == "divider" for block in result), 1)
        self.assertEqual(result[0]["type"], "paragraph")


if __name__ == "__main__":
    unittest.main()
