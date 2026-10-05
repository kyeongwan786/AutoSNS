"""
네이버 블로그 자동화 실행 파일.

config.json 에서 켜진 기능(buddy / like / comment)만 대상마다 실행합니다.
대상 하나당 순서: 글 열기 → 공감 → 댓글 → 서이추

실행:
    python main.py              # 실제 실행
    python main.py --test 3     # 테스트: 새 대상 3명만, 창 띄우고, 빠르게

설치:
    pip install playwright openai
    playwright install chromium
"""
from __future__ import annotations
import argparse
import asyncio
import random
import sys

from playwright.async_api import Page, async_playwright

from core.config import load_config
from core.llm import resolve_api_key
from core.post import open_post
from core.session import (
    LoginCompletedError, backup_cookies, close_naver, forget_cookies, open_naver,
)
from core.storage import Store, add_today, today_count
from core.utils import DialogCatcher, LoggedOutError, human_wait, set_speed
from features import buddy, comment, discover, like

FEATURES = {"like": like, "comment": comment, "buddy": buddy}
ORDER = ["like", "comment", "buddy"]


class Runner:
    def __init__(self, page: Page, cfg: dict, store: Store):
        self.page = page
        self.cfg = cfg
        self.store = store
        self.dialogs = DialogCatcher(page)
        self.stopped: set[str] = set()     # 네이버 한도에 걸린 기능
        self.since_break = 0
        self.next_break = random.randint(*cfg["general"]["break_every"])

    # ---------- 기능 상태 ----------
    def enabled(self) -> list[str]:
        return [f for f in ORDER if self.cfg[f]["enabled"]]

    def active(self, f: str) -> bool:
        return (self.cfg[f]["enabled"]
                and f not in self.stopped
                and today_count(f) < self.cfg[f]["daily_limit"])

    def any_active(self) -> bool:
        return any(self.active(f) for f in ORDER)

    def todo(self, blog_id: str) -> list[str]:
        return [f for f in ORDER if self.active(f) and not self.store.is_finished(blog_id, f)]

    # ---------- 결과 기록 ----------
    def record(self, blog_id: str, f: str, status: str, detail: str) -> bool:
        if status == "limit":
            self.stopped.add(f)
            print(f"    {FEATURES[f].LABEL}: 네이버 한도 도달, 오늘은 이 기능 중지 ({detail})")
            return False
        self.store.mark(blog_id, f, status, detail)
        if status == "done":
            add_today(f)
        count = f"(오늘 {today_count(f)}/{self.cfg[f]['daily_limit']})" if status == "done" else ""
        print(f"    {FEATURES[f].LABEL}: {status} {detail} {count}")
        return status == "done"

    # ---------- 대상 하나 처리 ----------
    async def process(self, blog_id: str) -> bool:
        """하나라도 실제로 했으면 True"""
        todo = self.todo(blog_id)
        if not todo:
            return False
        acted = False

        if "like" in todo or "comment" in todo:
            post = None
            try:
                post = await open_post(self.page, blog_id, self.store.targets[blog_id].get("post", ""))
            except LoggedOutError:
                raise
            except Exception as e:
                print(f"    글 열기 실패: {str(e)[:100]}")

            if post is None:
                for f in ("like", "comment"):
                    if f in todo:
                        self.record(blog_id, f, "skipped", "글을 열 수 없음")
            else:
                if "like" in todo:
                    try:
                        st, d = await like.run(self.page)
                    except LoggedOutError:
                        raise
                    except Exception as e:
                        st, d = "error", str(e)[:150]
                    acted |= self.record(blog_id, "like", st, d)

                if "comment" in todo:
                    try:
                        st, d = await comment.run(self.page, post, self.cfg["comment"], self.dialogs)
                    except LoggedOutError:
                        raise
                    except Exception as e:
                        st, d = "error", str(e)[:150]
                    acted |= self.record(blog_id, "comment", st, d)
                await human_wait(1500, 3000)

        if "buddy" in todo:
            try:
                st, d = await buddy.run(self.page, blog_id, self.cfg["buddy"], self.dialogs)
            except LoggedOutError:
                raise
            except Exception as e:
                st, d = "error", str(e)[:150]
            acted |= self.record(blog_id, "buddy", st, d)

        return acted

    async def rest(self, acted: bool) -> None:
        g = self.cfg["general"]
        if not acted:
            await asyncio.sleep(random.randint(*g["skip_delay_range"]))
            return
        self.since_break += 1
        if self.since_break >= self.next_break:
            sec = random.randint(*g["break_range"])
            print(f"  ☕ {sec // 60}분 휴식")
            await asyncio.sleep(sec)
            self.since_break = 0
            self.next_break = random.randint(*g["break_every"])
        else:
            sec = random.randint(*g["delay_range"])
            if sec > 0:
                print(f"  ⏳ {sec}초 대기")
                await asyncio.sleep(sec)

    async def run_ids(self, blog_ids: list[str]) -> None:
        for i, blog_id in enumerate(blog_ids, 1):
            if not self.any_active():
                return
            if not self.todo(blog_id):
                continue
            print(f"  [{i}/{len(blog_ids)}] {blog_id}")
            acted = await self.process(blog_id)
            await self.rest(acted)

    # ---------- 전체 흐름 ----------
    async def run(self, test_count: int | None = None) -> None:
        if test_count is None:
            leftover = [b for b in self.store.targets if self.store.needs_work(b, self.enabled())]
            if leftover:
                print(f"\n📌 지난번에 남은 대상 {len(leftover)}개부터 처리합니다.")
                await self.run_ids(leftover)

        done_new = 0
        for n in range(1, self.cfg["discover"]["max_pages"] + 1):
            if not self.any_active():
                break
            new_ids = await discover.collect_page(self.page, n, self.store, self.cfg["discover"])
            if not new_ids:
                await human_wait(2000, 4000)
                continue
            if test_count is not None:
                new_ids = new_ids[: test_count - done_new]
            await self.run_ids(new_ids)
            done_new += len(new_ids)
            if test_count is not None and done_new >= test_count:
                break


def print_status(cfg: dict) -> None:
    print("\n=== 작업 설정 ===")
    for f in ORDER:
        mod = FEATURES[f]
        on = "ON " if cfg[f]["enabled"] else "OFF"
        print(f"  {mod.LABEL:<8} [{on}]  오늘 {today_count(f)}/{cfg[f]['daily_limit']}")
    print()


async def main(test_count: int | None) -> None:
    cfg = load_config()

    if test_count is not None:
        cfg["general"].update({
            "show_browser": True,
            "delay_range": [0, 0],          # 대상 사이 대기 없음
            "skip_delay_range": [0, 0],
            "break_every": [10_000, 10_000],
        })
        set_speed(0.15)  # 페이지 로딩/반영 확인용 최소 대기만 남김
        print(f"🧪 테스트 모드: 새 대상 {test_count}명만 처리합니다.")

    if cfg["comment"]["enabled"] and not resolve_api_key(cfg["comment"]):
        print("⚠️ OpenAI API 키가 없어서 댓글 기능은 끄고 진행합니다.")
        cfg["comment"]["enabled"] = False

    print_status(cfg)
    if not any(cfg[f]["enabled"] for f in ORDER):
        print("켜진 기능이 없습니다. config.json 에서 enabled 를 true 로 바꿔주세요.")
        return

    store = Store()
    logged_out = False
    async with async_playwright() as p:
        try:
            context, page = await open_naver(p, cfg["general"], stop_after_login=True)
        except LoginCompletedError:
            return
        except BaseException:
            await close_naver()   # 로그인 단계에서 실패해도 크롬은 정리
            raise
        runner = Runner(page, cfg, store)
        try:
            if not runner.any_active():
                print("켜진 기능이 모두 오늘 한도에 도달했습니다.")
            else:
                await runner.run(test_count)
        except LoggedOutError:
            logged_out = True
            print("\n🚪 진행 중 로그아웃이 감지되어 멈췄습니다.")
            print("   다시 실행하면 로그인 창이 뜹니다. '로그인 상태 유지'를 체크해서 로그인해 주세요.")
        finally:
            if logged_out:
                forget_cookies()
            else:
                try:
                    await backup_cookies(context)
                except Exception:
                    pass
            await close_naver()

    print_status(cfg)


async def login_only() -> None:
    """Prepare and save the Naver login without starting automation."""
    cfg = load_config()
    async with async_playwright() as p:
        try:
            context, _page = await open_naver(p, cfg["general"])
            await backup_cookies(context)
        finally:
            await close_naver()
    print("✅ 네이버 로그인 준비 완료. 작업은 대시보드에서 '작업 시작'을 눌러주세요.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="네이버 블로그 자동화")
    parser.add_argument("--test", type=int, metavar="N", help="테스트 모드: 새 대상 N명만 처리")
    parser.add_argument("--ui", action="store_true", help="웹 UI 실행")
    parser.add_argument("--login-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--automation-child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.login_only:
        asyncio.run(login_only())
    elif args.automation_child or args.test is not None:
        asyncio.run(main(args.test))
    elif args.ui or getattr(sys, "frozen", False):
        from ui import launch
        launch()
    else:
        asyncio.run(main(None))
