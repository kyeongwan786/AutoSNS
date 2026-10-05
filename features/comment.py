"""자동 댓글: 글 내용으로 LLM 댓글 생성 후 댓글 페이지에서 등록"""
from __future__ import annotations

from playwright.async_api import Page

from core.llm import make_comment
from core.post import Post
from core.utils import DialogCatcher, assert_logged_in, human_wait, typing_delay

NAME = "comment"
LABEL = "💬 댓글"

COMMENT_URL = "https://m.blog.naver.com/CommentList.naver?blogId={blog_id}&logNo={log_no}"

SEL_WRITE_BOX = ".u_cbox_write"                                  # 댓글 쓰기 영역 전체
SEL_COMMENT_INPUT = "#naverComment__write_textarea, .u_cbox_inbox .u_cbox_text"
SEL_COMMENT_GUIDE = ".u_cbox_inbox .u_cbox_guide"                # '댓글을 입력해주세요.' 안내
SEL_COMMENT_SUBMIT = ".u_cbox_write .u_cbox_btn_upload"
SEL_COMMENT_LIST = ".u_cbox_contents"                            # 등록된 댓글 본문들


async def _focus_input(page: Page):
    """입력창 활성화. 가려져 있어도 스크롤 재시도 루프에 빠지지 않도록 짧은 타임아웃 + 직접 포커스"""
    write = page.locator(SEL_WRITE_BOX).first
    await write.wait_for(state="attached", timeout=8_000)
    await write.scroll_into_view_if_needed(timeout=3_000)
    await human_wait(300, 700)

    # 1) 안내 문구 클릭 (입력창 활성화 트리거)
    guide = page.locator(SEL_COMMENT_GUIDE).first
    try:
        await guide.click(timeout=2_000)
    except Exception:
        pass
    await human_wait(300, 700)

    # 2) 입력창에 직접 포커스
    box = page.locator(SEL_COMMENT_INPUT).first
    await box.wait_for(state="attached", timeout=5_000)
    try:
        await box.click(timeout=2_000)
    except Exception:
        await box.evaluate("el => el.focus()")
    return box


async def run(page: Page, post: Post, cfg: dict, dialogs: DialogCatcher) -> tuple[str, str]:
    """반환: (status, detail)  status = done | skipped | error"""
    if not post.body.strip():
        return "skipped", "본문 못 읽음"

    try:
        text = await make_comment(cfg, post.title, post.body)
    except Exception as e:
        return "error", f"LLM 호출 실패 ({str(e)[:80]})"
    if not text:
        return "skipped", "광고/내용 부족"

    await page.goto(COMMENT_URL.format(blog_id=post.blog_id, log_no=post.log_no),
                    wait_until="domcontentloaded")
    await human_wait(1500, 3000)
    assert_logged_in(page)

    try:
        box = await _focus_input(page)
    except Exception:
        return "skipped", "댓글창 없음 (댓글 막힌 글이거나 화면 구조 다름)"

    dialogs.clear()
    # 포커스된 입력창에 키보드로 입력 (클릭 재시도 없음)
    await page.keyboard.type(text, delay=typing_delay())
    await human_wait(800, 1800)
    assert_logged_in(page)

    typed = (await box.inner_text()).strip()
    if text[:10] not in typed:
        return "error", f"입력이 안 됨 (입력창 내용: '{typed[:30]}')"

    try:
        await page.locator(SEL_COMMENT_SUBMIT).first.click(timeout=3_000)
    except Exception:
        return "error", "등록 버튼을 못 누름"
    await human_wait(2000, 3500)
    assert_logged_in(page)

    if dialogs.last:
        return "skipped", dialogs.last

    # 실제로 목록에 올라갔는지 확인
    try:
        bodies = await page.locator(SEL_COMMENT_LIST).all_inner_texts()
    except Exception:
        bodies = []
    if any(text[:15] in b for b in bodies):
        return "done", text
    return "error", f"등록 확인 안 됨 (입력한 댓글: {text})"
