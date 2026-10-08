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


def _configure_console_output() -> None:
    """Prevent Windows legacy console encodings from crashing on emoji labels."""
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            # The UI captures child-process output as UTF-8. Keep a directly
            # attached terminal's configured encoding, but replace characters
            # it cannot represent instead of raising UnicodeEncodeError.
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


_configure_console_output()

from playwright.async_api import Page, async_playwright

from core.config import load_config
from core.llm import resolve_api_key
from core.post import open_post
from core.session import (
    LoginCompletedError, NaverCredentialError, backup_cookies, close_naver, forget_cookies, open_naver,
)
from core.storage import Store, add_today, today_count, today_total
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
        self.global_stopped = False        # 계정 전체 작업을 중단하는 플랫폼 제한
        self.since_break = 0
        self.next_break = random.randint(*cfg["general"]["break_every"])

    # ---------- 기능 상태 ----------
    def enabled(self) -> list[str]:
        return [f for f in ORDER if self.cfg[f]["enabled"]]

    def active(self, f: str) -> bool:
        individual = self.cfg["general"].get("daily_limit_mode") == "individual"
        within_limit = (today_count(f) < self.cfg[f]["daily_limit"] if individual else
                        today_total() < self.cfg["general"]["daily_task_limit"])
        return (not self.global_stopped
                and self.cfg[f]["enabled"]
                and f not in self.stopped
                and within_limit)

    def any_active(self) -> bool:
        return any(self.active(f) for f in ORDER)

    def todo(self, blog_id: str) -> list[str]:
        return [f for f in ORDER if self.active(f) and not self.store.is_finished(blog_id, f)]

    # ---------- 결과 기록 ----------
    def record(self, blog_id: str, f: str, status: str, detail: str) -> bool:
        if status == "global_limit":
            self.global_stopped = True
            self.store.mark(blog_id, f, "limit", detail)
            print(f"    네이버 계정 제한 알림 감지: 모든 자동 작업을 중지합니다 ({detail})")
            return False
        if status == "limit":
            self.stopped.add(f)
            print(f"    {FEATURES[f].LABEL}: 네이버 한도 도달, 오늘은 이 기능 중지 ({detail})")
            return False
        self.store.mark(blog_id, f, status, detail)
        if status == "done":
            add_today(f)
        if status == "done":
            if self.cfg["general"].get("daily_limit_mode") == "individual":
                count = f"(오늘 {today_count(f)}/{self.cfg[f]['daily_limit']})"
            else:
                count = f"(오늘 전체 {today_total()}/{self.cfg['general']['daily_task_limit']})"
        else:
            count = ""
        print(f"    {FEATURES[f].LABEL}: {status} {detail} {count}")
        return status == "done"

    # ---------- 대상 하나 처리 ----------
    async def process(self, blog_id: str) -> bool:
        """하나라도 실제로 했으면 True"""
        todo = self.todo(blog_id)
        if not todo:
            return False
        acted = False

        if ("like" in todo and self.active("like")) or ("comment" in todo and self.active("comment")):
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
                if "like" in todo and self.active("like"):
                    try:
                        st, d = await like.run(self.page)
                    except LoggedOutError:
                        raise
                    except Exception as e:
                        st, d = "error", str(e)[:150]
                    acted |= self.record(blog_id, "like", st, d)

                if "comment" in todo and self.active("comment"):
                    try:
                        st, d = await comment.run(self.page, post, self.cfg["comment"], self.dialogs)
                    except LoggedOutError:
                        raise
                    except Exception as e:
                        st, d = "error", str(e)[:150]
                    acted |= self.record(blog_id, "comment", st, d)
                await human_wait(1500, 3000)

        if "buddy" in todo and self.active("buddy"):
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
            print(f"  ☕ {max(1, round(sec / 60))}분 휴식")
            await asyncio.sleep(sec)
            self.since_break = 0
            self.next_break = random.randint(*g["break_every"])
        else:
            sec = random.randint(*g["delay_range"])
            if sec > 0:
                print(f"  ⏳ {max(1, round(sec))}초 대기")
                await asyncio.sleep(sec)

    async def run_ids(self, blog_ids: list[str]) -> None:
        for i, blog_id in enumerate(blog_ids, 1):
            if not self.any_active():
                return
            if not self.todo(blog_id):
                continue
            print(f"  [{i}/{len(blog_ids)}] {blog_id}")
            acted = await self.process(blog_id)
            if self.global_stopped or not self.any_active():
                return
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
    individual = cfg["general"].get("daily_limit_mode") == "individual"
    for f in ORDER:
        mod = FEATURES[f]
        on = "ON " if cfg[f]["enabled"] else "OFF"
        limit = f"/{cfg[f]['daily_limit']}" if individual else "회 완료"
        print(f"  {mod.LABEL:<8} [{on}]  오늘 {today_count(f)}{limit}")
    if individual:
        print("  한도 방식: 기능별 개별 설정")
    else:
        print(f"  하루 전체 한도: {today_total()}/{cfg['general']['daily_task_limit']}회")
    print()


async def main(test_count: int | None) -> None:
    cfg = load_config()
    speed_profiles = {
        "min": {"factor": 1.0, "delay_range": [40, 120], "break_every": [8, 12],
                "break_range": [300, 600], "skip_delay_range": [5, 15]},
        "medium": {"factor": 0.7, "delay_range": [20, 60], "break_every": [12, 18],
                   "break_range": [180, 360], "skip_delay_range": [3, 8]},
        "max": {"factor": 0.45, "delay_range": [8, 25], "break_every": [18, 25],
                "break_range": [90, 180], "skip_delay_range": [1, 4]},
    }
    profile = speed_profiles.get(cfg["general"].get("task_speed"), speed_profiles["medium"])
    cfg["general"].update({key: value for key, value in profile.items() if key != "factor"})
    set_speed(profile["factor"])

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


async def login_only(credentials: dict | None = None) -> None:
    """Prepare and save the Naver login without starting automation."""
    cfg = load_config()
    async with async_playwright() as p:
        try:
            context, _page = await open_naver(p, cfg["general"], stop_after_login=True, credentials=credentials)
            await backup_cookies(context)
        except LoginCompletedError:
            # The CDP flow saved the account cookies, closed its verification
            # browser and signals completion through this exception.
            pass
        finally:
            await close_naver()
            if credentials is not None:
                credentials["username"] = ""
                credentials["password"] = ""
    print("✅ 네이버 로그인 준비 완료. 작업은 대시보드에서 '작업 시작'을 눌러주세요.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="네이버 블로그 자동화")
    parser.add_argument("--test", type=int, metavar="N", help="테스트 모드: 새 대상 N명만 처리")
    parser.add_argument("--ui", action="store_true", help="웹 UI 실행")
    parser.add_argument("--login-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--login-only-credentials", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--automation-child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.login_only:
        credentials = None
        if args.login_only_credentials:
            try:
                import json
                credentials = json.loads(sys.stdin.readline())
                if not isinstance(credentials, dict):
                    credentials = None
            except Exception:
                credentials = None
        try:
            asyncio.run(login_only(credentials))
        except NaverCredentialError:
            print("네이버 아이디 또는 비밀번호가 맞지 않습니다.")
            raise SystemExit(2)
    elif args.automation_child or args.test is not None:
        asyncio.run(main(args.test))
    elif args.ui or getattr(sys, "frozen", False):
        from ui import launch
        launch()
    else:
        asyncio.run(main(None))
