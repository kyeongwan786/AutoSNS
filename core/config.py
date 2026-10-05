"""
config.json 읽기/쓰기.
파일이 없으면 기본값으로 새로 만들고, 일부 항목만 있어도 나머지는 기본값으로 채웁니다.
나중에 GUI는 이 파일만 읽고 쓰면 됩니다.
"""
from __future__ import annotations
import copy
import json
import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    if sys.platform == "win32":
        BASE_DIR = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "AutoSNS"
    elif sys.platform == "darwin":
        BASE_DIR = Path.home() / "Library" / "Application Support" / "AutoSNS"
    else:
        BASE_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "AutoSNS"
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
    RESOURCE_DIR = BASE_DIR
BASE_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = BASE_DIR / "config.json"


DEFAULT_COMMENT_PROMPT = """아래 블로그 글을 읽고, 글 내용에 실제로 반응하는 짧은 댓글을 작성해.

작성 원칙:
- 글에서 확인되는 구체적인 내용 하나를 짚고, 그 내용에 대한 짧은 반응을 덧붙여.
- 매번 같은 도입이나 마무리를 쓰지 말고, 문장 구조와 길이를 자연스럽게 바꿔. 억지로 구어체나 유행어를 넣지 마.
- 존댓말로 1~2문장, 보통 20~60자 정도로 작성해. 글에 맞으면 이모지를 하나 쓸 수 있어.
- 글에 없는 사실이나 직접 해본 경험을 지어내지 말고, 과한 칭찬이나 요약문처럼 쓰지 마.
- "잘 보고 갑니다", "유익한 정보 감사합니다", "인상적이네요" 같은 상투적인 문구와 서로이웃/홍보 언급은 쓰지 마.
- 광고·협찬 글이거나 댓글을 달 만한 구체적인 내용이 없으면 SKIP만 출력해.
- 댓글 문장만 출력하고 따옴표나 설명은 붙이지 마.

제목: {title}
본문: {body}"""

DEFAULTS = {
    "general": {
        "browser_mode": "cdp",          # "cdp" = 일반 크롬 직접 실행 후 연결 / "playwright" = 내장 브라우저
        "chrome_path": "",              # 비워두면 자동으로 찾음
        "cdp_port": 9222,
        "show_browser": False,          # True면 작업 중 브라우저 창 보이기
        "use_chrome": False,            # playwright 모드에서만: 설치된 크롬 사용
        "delay_range": [40, 120],       # 대상 사이 대기 (초)
        "break_every": [8, 12],         # 이 범위의 대상 수마다
        "break_range": [300, 600],      # 긴 휴식 (초)
        "skip_delay_range": [5, 15],    # 아무것도 안 했을 때 대기 (초)
    },
    "discover": {
        "theme_url": "https://section.blog.naver.com/ThemePost.naver?directoryNo=0&activeDirectorySeq=0&currentPage={page}",
        "max_pages": 10,
    },
    "buddy": {
        "enabled": True,
        "daily_limit": 100,
        "messages": [
            "안녕하세요! 글 잘 보고 갑니다. 서로이웃 하고 소통해요 :)",
            "포스팅 재밌게 읽었어요~ 서로이웃 신청드립니다!",
            "우연히 들어왔다가 글이 좋아서 신청드려요. 자주 놀러올게요!",
            "안녕하세요 :) 비슷한 관심사가 있어서 서이추 신청합니다~",
            "좋은 글 감사합니다! 서로이웃으로 자주 소통하면 좋겠어요.",
            "블로그 구경 잘 하고 갑니다~ 이웃하고 지내요!",
            "글 분위기가 좋아서 서로이웃 신청드려요. 좋은 하루 보내세요!",
            "안녕하세요! 앞으로 글 자주 보고 싶어서 서이추 신청합니다 :)",
        ],
    },
    "like": {
        "enabled": True,
        "daily_limit": 100,
    },
    "comment": {
        "enabled": True,
        "daily_limit": 100,
        "model": "gpt-4o-mini",
        "api_key": "",                  # 비워두면 환경변수 OPENAI_API_KEY 사용
        "max_body_chars": 1500,
        "prompt": DEFAULT_COMMENT_PROMPT,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        save_config(DEFAULTS)
        print(f"📝 기본 설정 파일을 만들었습니다: {CONFIG_FILE.name}")
        config = copy.deepcopy(DEFAULTS)
    else:
        user = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        config = _merge(DEFAULTS, user)

    return config


def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
