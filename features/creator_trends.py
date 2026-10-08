"""Collect Creator Advisor topic trends and rank post ideas with GPT-4o mini."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime

from core.config import load_config
from core.credentials import load_naver_credentials
from core.session import LoginCompletedError, close_naver, open_naver
from core.storage import data_file, save_json

CREATOR_TRENDS_URL = "https://creator-advisor.naver.com/trends/naver_blog#trend-by-categories"
TREND_CATEGORIES = (
    "IT·컴퓨터", "건강·의학", "게임", "공연·전시", "교육·학문", "국내여행", "드라마",
    "만화·애니", "맛집", "문학·책", "미술·디자인", "반려동물", "방송", "비즈니스·경제",
    "사진", "사회·정치", "상품리뷰", "세계여행", "스타·연예인", "스포츠", "어학·외국어",
    "영화", "요리·레시피", "원예·재배", "육아·결혼", "음악", "인테리어·DIY",
    "일상·생각", "자동차", "좋은글·이미지", "취미", "패션·미용",
)


def _group_topic_categories(lines: list[str]) -> list[dict]:
    known = {re.sub(r"\s+", "", name).replace("·", "").casefold(): name for name in TREND_CATEGORIES}
    categories = []
    current = None
    for line in lines:
        normalized = re.sub(r"\s+", "", line).replace("·", "").casefold()
        if normalized in known:
            current = {"category": known[normalized], "trends": []}
            categories.append(current)
        elif current is not None:
            current["trends"].append(line)
    return [entry for entry in categories if entry["trends"]]


async def _collect_visible_trend_data(page) -> dict:
    body = await page.locator("body").inner_text(timeout=10_000)
    lines = []
    for line in body.splitlines():
        clean = re.sub(r"\s+", " ", line).strip()
        if clean and (not lines or clean != lines[-1]):
            lines.append(clean[:240])
    categories = []
    swiper_containers = page.locator(".swiper-uni_search_swiper")
    for container_index in range(await swiper_containers.count()):
        container = swiper_containers.nth(container_index)
        try:
            state = await container.evaluate("""el => {
              const swiper = el.swiper;
              return {
                active: swiper ? (swiper.params.loop ? swiper.realIndex : swiper.activeIndex) : 0,
                count: swiper ? swiper.slides.length : el.querySelectorAll('.swiper-slide').length,
                loop: Boolean(swiper && swiper.params.loop),
                looped: swiper ? (swiper.loopedSlides || 0) : 0
              };
            }""")
            total = int(state.get("count", 0))
            if state.get("loop"):
                total = max(0, total - 2 * int(state.get("looped", 0)))
            for slide_index in range(total):
                await container.evaluate("""(el, index) => {
                  const swiper = el.swiper;
                  if (!swiper) return;
                  if (swiper.params.loop && swiper.slideToLoop) swiper.slideToLoop(index, 0, false);
                  else swiper.slideTo(index, 0, false);
                }""", slide_index)
                await page.wait_for_timeout(100)
                active = container.locator(".swiper-slide-active").first
                if not await active.count():
                    continue
                category = ""
                heading = active.locator(".u_ni_trend_list_box h3, .u_ni_trend_list h3, h3").first
                if await heading.count():
                    category = (await heading.inner_text()).strip()
                keyword_items = await active.locator("li.u_ni_trend_item").evaluate_all("""items => items.map(item => {
                  const link = item.querySelector('a.u_ni_trend_link');
                  const keyword = (link?.innerText || item.innerText || '').replace(/\\s+/g, ' ').trim();
                  const signals = (item.innerText || '').replace(/\\s+/g, ' ').trim();
                  return keyword ? {keyword, signals} : null;
                }).filter(Boolean)""")
                if category and keyword_items:
                    existing = next((entry for entry in categories if entry["category"] == category), None)
                    if existing is None:
                        existing = {"category": category, "keywords": []}
                        categories.append(existing)
                    seen_keywords = {item["keyword"] for item in existing["keywords"]}
                    existing["keywords"].extend(item for item in keyword_items if item["keyword"] not in seen_keywords)
            await container.evaluate("""(el, index) => {
              const swiper = el.swiper;
              if (!swiper) return;
              if (swiper.params.loop && swiper.slideToLoop) swiper.slideToLoop(index, 0, false);
              else swiper.slideTo(index, 0, false);
            }""", int(state.get("active", 0)))
        except Exception:
            continue

    if not categories:
        categories = _group_topic_categories(lines)
    return {"page_lines": lines, "categories": categories}


async def scrape_creator_trends(account_id: str, chrome_path: str = "") -> dict:
    """Read every topic category and trend already rendered on the Advisor page."""
    from playwright.async_api import async_playwright

    general = load_config().get("general", {})
    if chrome_path:
        general = {**general, "chrome_path": chrome_path}
    try:
        credentials = load_naver_credentials(account_id)
    except Exception:
        credentials = None
    async with async_playwright() as playwright:
        if credentials and general.get("browser_mode", "cdp") == "cdp":
            try:
                await open_naver(playwright, general, stop_after_login=True,
                                 credentials=credentials, account_id=account_id)
            except LoginCompletedError:
                pass
            finally:
                credentials["username"] = ""
                credentials["password"] = ""
        context, page = await open_naver(playwright, general, account_id=account_id)
        try:
            await page.goto(CREATOR_TRENDS_URL, wait_until="domcontentloaded", timeout=35_000)
            await page.wait_for_timeout(3500)
            if "nid.naver.com" in page.url.lower() or "introduction" in page.url.lower():
                raise RuntimeError("연결된 네이버 계정으로 크리에이터 어드바이저에 접근할 수 없어요. 계정 로그인과 서비스 이용 상태를 확인해 주세요.")

            initial = await _collect_visible_trend_data(page)
            initial_text = "\n".join(initial["page_lines"])
            if await page.locator('input[type="password"], form[action*="login"]').count() or (
                "로그인" in initial_text and not any(word in initial_text for word in ("로그아웃", "로그인 정보", "로그인 상태"))
            ):
                raise RuntimeError("연결된 네이버 계정의 로그인이 필요한 화면이 열렸어요. 다시 로그인한 뒤 추천을 시도해 주세요.")
            if len(initial_text) < 120 or not any(word in initial_text for word in ("트렌드", "검색어", "인기")):
                raise RuntimeError("트렌드 데이터를 화면에서 찾지 못했어요. 네이버 로그인 상태와 어드바이저 화면을 확인해 주세요.")
            if not initial["categories"]:
                raise RuntimeError("트렌드 검색어 목록을 찾지 못했어요. 어드바이저 화면 구성이 바뀌었는지 확인해 주세요.")

            return {
                "source": CREATOR_TRENDS_URL,
                "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "categories_collected": len(initial["categories"]),
                "categories": initial["categories"],
                "page_lines": initial["page_lines"],
            }
        finally:
            await close_naver()


async def recommend_topics(account_id: str, context_keywords: list[str] | None, openai_api_key: str,
                           use_cloud: bool = False, access_token: str | None = None) -> dict:
    """Analyze all available Creator Advisor topic trends; no user keyword is required."""
    if not openai_api_key and not use_cloud:
        raise RuntimeError("GPT 추천을 사용하려면 config.json의 comment.api_key 또는 OPENAI_API_KEY 환경 변수를 설정해 주세요.")
    trend_data = await scrape_creator_trends(account_id, load_config().get("general", {}).get("chrome_path", ""))

    if use_cloud:
        from core import cloud
        result = await asyncio.to_thread(cloud.generate_post_topics, trend_data, [], access_token)
        items = result.get("recommendations", []) if isinstance(result, dict) else []
        result = {
            "recommendations": _clean_recommendations(items, trend_data),
            "source": "네이버 크리에이터 어드바이저 트렌드",
            "model": "gpt-4o-mini",
            "categories_collected": trend_data.get("categories_collected", 0),
        }
        _save_analysis(account_id, trend_data, result)
        return result

    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=openai_api_key, timeout=60.0, max_retries=1)
    prompt = f"""아래는 네이버 크리에이터 어드바이저 트렌드 화면에서 수집한 전체 카테고리·키워드·콘텐츠 제목 자료입니다.
이 자료 전체를 분석해, 그중 지금 포스팅했을 때 조회 관심과 이슈성이 높을 가능성이 있는 항목만 점수순으로 추천하세요. 카테고리별 개수를 맞추거나 분야를 억지로 분산하지 마세요. 한 분야에서 실제 신호가 강하면 같은 분야가 여러 개여도 됩니다.
익숙한 분야라는 이유만으로 선택하지 말고, 순위 상승, 신규·급상승 표시, 구체적인 이슈 제목, 최근성처럼 자료에서 확인되는 신호를 기준으로 판단하세요. 상시 인기 키워드는 현재 상승 근거가 약하면 낮게 평가하세요.
실제 조회수·검색량 수치가 없으면 만들어내지 말고, 0~100은 후보 간 상대적인 조회 관심·이슈 가능성 점수로 매기세요. 근거가 약한 항목은 제외하고 유망한 후보를 최대 10개 반환하세요.
검색어가 나온 카테고리, 점수 근거, 바로 활용할 구체적인 포스팅 제목 방향을 함께 적으세요.
JSON 객체만 반환: {{"recommendations":[{{"keyword":"수집된 검색어","category":"수집된 카테고리","opportunity_score":82,"reason":"어떤 상승·이슈 근거로 유망한지","topic":"구체적인 포스팅 제목 또는 구성 방향"}}]}}

크리에이터 어드바이저에서 수집한 전체 트렌드(JSON):
{json.dumps(trend_data, ensure_ascii=False)}
"""
    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.45,
        max_tokens=1800,
    )
    try:
        parsed = json.loads(response.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        raise RuntimeError("GPT 추천 결과를 읽지 못했어요. 다시 시도해 주세요.") from None
    items = parsed.get("recommendations", []) if isinstance(parsed, dict) else []
    result = {
        "recommendations": _clean_recommendations(items, trend_data),
        "source": "네이버 크리에이터 어드바이저 트렌드",
        "model": "gpt-4o-mini",
        "categories_collected": trend_data.get("categories_collected", 0),
    }
    _save_analysis(account_id, trend_data, result)
    return result


async def recommend_related_topics(keyword: str, openai_api_key: str, use_cloud: bool = False,
                                   access_token: str | None = None) -> dict:
    """Suggest practical blog angles based on a user-entered keyword."""
    keyword = str(keyword or "").strip()[:40]
    if not keyword:
        raise RuntimeError("연관 주제를 찾을 키워드를 입력해 주세요.")
    search_evidence_count = 0
    if use_cloud:
        from core import cloud
        result = await asyncio.to_thread(
            cloud.generate_post_topics,
            {"source": "사용자 입력 키워드", "request_type": "related_topics", "keyword": keyword,
             "categories": [{"category": "연관 주제", "keywords": [keyword]}]},
            [keyword],
            access_token,
        )
        items = result.get("recommendations", []) if isinstance(result, dict) else []
        recommendations = _clean_related_recommendations(items, keyword)
        search_evidence_count = result.get("search_evidence_count", 0)
    else:
        if not openai_api_key:
            raise RuntimeError("키워드 기반 주제 추천을 사용하려면 config.json의 comment.api_key 또는 OPENAI_API_KEY 환경 변수를 설정해 주세요.")
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=openai_api_key, timeout=60.0, max_retries=1)
        prompt = f"""사용자가 입력한 블로그 키워드와 관련해 독자가 실제로 궁금해할 만한 포스팅 주제를 한국어로 추천하세요.
키워드의 표현만 바꾼 중복 항목은 만들지 말고, 비교·사용법·문제 해결·비용·선택 기준처럼 서로 다른 검색 의도를 반영하세요. 키워드가 장소나 상품이어도 방문·구매 경험을 지어내지 마세요. 현재 인기나 검색량을 확인하지 않았으므로 트렌드라고 주장하거나 수치를 만들지 마세요.
최대 8개를 반환하세요. 각 항목은 검색에 쓸 만한 구체적인 키워드, 독자가 궁금해할 이유, 그 키워드로 쓸 수 있는 포스팅 방향을 담아야 합니다. JSON 객체만 반환하세요.
{{"recommendations":[{{"keyword":"구체적인 연관 키워드","category":"비교/사용법/문제 해결/비용/선택 기준 중 하나","opportunity_score":0,"reason":"독자가 이 주제를 찾을 만한 이유","topic":"구체적인 포스팅 방향"}}]}}

입력 키워드: {keyword}
"""
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.65,
            max_tokens=1800,
        )
        try:
            parsed = json.loads(response.choices[0].message.content or "{}")
        except json.JSONDecodeError:
            raise RuntimeError("연관 주제 추천 결과를 읽지 못했어요. 다시 시도해 주세요.") from None
        items = parsed.get("recommendations", []) if isinstance(parsed, dict) else []
        recommendations = _clean_related_recommendations(items, keyword)
    return {
        "recommendations": recommendations,
        "source": "네이버 검색 결과 기반 연관 주제" if use_cloud else "입력 키워드 기반 연관 주제",
        "context_keyword": keyword,
        "model": "gpt-4o-mini",
        "search_grounded": use_cloud,
        "search_evidence_count": search_evidence_count,
    }


def _clean_related_recommendations(items: list, source_keyword: str) -> list[dict]:
    cleaned, seen = [], {source_keyword.casefold()}
    for item in items[:8] if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        keyword = str(item.get("keyword", "")).strip()[:100]
        if not keyword or keyword.casefold() in seen:
            continue
        seen.add(keyword.casefold())
        cleaned.append({
            "keyword": keyword,
            "category": str(item.get("category", "연관 주제")).strip()[:60] or "연관 주제",
            "opportunity_score": 0,
            "reason": str(item.get("reason", "")).strip()[:300],
            "topic": str(item.get("topic", "")).strip()[:300],
        })
    return cleaned


def _clean_recommendations(items: list, trend_data: dict) -> list[dict]:
    source_text = json.dumps(trend_data, ensure_ascii=False).casefold()
    cleaned, seen = [], set()
    categories = trend_data.get("categories", []) if isinstance(trend_data, dict) else []
    for item in items[:10] if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        keyword = str(item.get("keyword", "")).strip()[:100]
        if not keyword or keyword.casefold() in seen or keyword.casefold() not in source_text:
            continue
        category = str(item.get("category", "")).strip()[:60]
        matching_category = next((
            str(entry.get("category", "")).strip()
            for entry in categories
            if isinstance(entry, dict) and keyword.casefold() in json.dumps(entry, ensure_ascii=False).casefold()
        ), "")
        if matching_category:
            category = matching_category
        try:
            score = max(0, min(100, int(item.get("opportunity_score", 0))))
        except (TypeError, ValueError):
            score = 0
        seen.add(keyword.casefold())
        cleaned.append({
            "keyword": keyword,
            "category": category or "인기 트렌드",
            "opportunity_score": score,
            "reason": str(item.get("reason", "")).strip()[:300],
            "topic": str(item.get("topic", "")).strip()[:300],
        })
    return sorted(cleaned, key=lambda item: item["opportunity_score"], reverse=True)


def _save_analysis(account_id: str, collected_data: dict, result: dict) -> None:
    """Persist the collected public trend data and analysis in this account's local folder."""
    save_json(data_file("creator_trends_analysis.json", account_id), {
        "analyzed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "collected_data": collected_data,
        **result,
    })
