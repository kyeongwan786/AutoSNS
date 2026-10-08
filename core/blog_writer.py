"""Generate structured Korean blog drafts and optional local illustrations."""
from __future__ import annotations

import base64
import json
import re
import shutil
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from core.storage import account_data_dir

MINIMUM_BODY_CHARACTERS = 3000


def _account_dir(account_id: str) -> Path:
    return account_data_dir(account_id)


def _clean_blocks(value: object, payload: dict | None = None) -> list[dict]:
    payload = payload if isinstance(payload, dict) else {}
    aliases = {"text": "paragraph", "body": "paragraph", "content": "paragraph", "paragraphs": "paragraph", "section": "paragraph", "subheading": "heading", "h2": "heading", "blockquote": "quote", "quotation": "quote", "photo": "image"}
    feature_types = {"text": "paragraph", "heading": "heading", "quotation": "quote", "divider": "divider", "table": "table", "photo": "image", "place": "place"}
    def clean_article_text(value: object) -> str:
        text = str(value or "").replace("\\*", "*").replace("\\_", "_").replace("\\-", "-").replace("\\.", ".")
        text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
        if re.match(r"\s*(?:Create a realistic|이미지 생성 프롬프트|image generation prompt)", text, re.IGNORECASE):
            return ""
        text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
        text = re.sub(r"(?m)^\s*(?:[-*+]\s+|\d+[.)]\s+)", "", text)
        text = re.sub(r"\*\*(.*?)\*\*|__(.*?)__", lambda match: match.group(1) or match.group(2), text)
        text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)|(?<!_)_([^_\n]+)_(?!_)", lambda match: match.group(1) or match.group(2), text)
        text = re.sub(r"\[([^\]]+)\]\(https?://[^)]+\)", r"\1", text)
        return re.sub(r"[ \t]+\n", "\n", text).strip()

    def extract_text(item: object, depth: int = 0) -> list[str]:
        if depth > 20:
            return []
        if isinstance(item, str):
            cleaned = clean_article_text(item)
            return [part.strip() for part in re.split(r"\n\s*\n", cleaned) if part.strip()]
        if isinstance(item, list):
            return [text for child in item for text in extract_text(child, depth + 1)]
        if isinstance(item, dict):
            found = []
            # Model responses sometimes wrap text in provider-specific node keys
            # (for example `plain_text` or nested `elements`). Walk the full block
            # payload, while ignoring schema metadata that could become prose.
            ignored = {"type", "kind", "block_type", "editor_feature", "style", "quote_style", "id"}
            for key, child in item.items():
                if str(key).lower() not in ignored:
                    found.extend(extract_text(child, depth + 1))
            return found
        return []

    def block_text(raw: dict) -> list[str]:
        for key in ("text", "content", "body", "paragraph", "paragraphs", "description", "value", "children", "nodes", "title", "heading", "runs"):
            if key in raw:
                found = extract_text(raw[key])
                if found:
                    return found
        return extract_text(raw)

    if isinstance(value, str):
        value = [{"type": "paragraph", "text": part} for part in re.split(r"\n\s*\n", value) if part.strip()]
    if not isinstance(value, list) or not value:
        # Accept common text-model shapes as well as the preferred block schema.
        alternate = next((payload[key] for key in ("content", "body", "article", "paragraphs", "article_body", "post_content", "body_html", "text") if key in payload), None)
        if isinstance(alternate, str):
            value = [{"type": "paragraph", "text": part} for part in re.split(r"\n\s*\n", alternate) if part.strip()]
        elif isinstance(alternate, list):
            value = alternate
        elif isinstance(payload.get("sections"), list):
            value = []
            for section in payload["sections"]:
                if isinstance(section, str):
                    value.append({"type": "paragraph", "text": section})
                elif isinstance(section, dict):
                    heading = str(section.get("heading", section.get("title", ""))).strip()
                    if heading:
                        value.append({"type": "heading", "text": heading})
                    content = section.get("paragraphs", section.get("content", section.get("text", "")))
                    value.extend({"type": "paragraph", "text": part} for part in extract_text(content))
        else:
            value = []
    allowed = {"paragraph", "heading", "quote", "divider", "table", "image", "place"}
    blocks: list[dict] = []
    for raw in value[:60]:
        if isinstance(raw, str):
            if raw.strip():
                blocks.append({"type": "paragraph", "text": raw.strip()[:5000]})
            continue
        if not isinstance(raw, dict):
            continue
        editor_feature = str(raw.get("editor_feature", "")).strip().lower()
        raw_kind = str(raw.get("type", raw.get("kind", raw.get("block_type", "")))).strip().lower()
        if editor_feature in feature_types:
            raw_kind = feature_types[editor_feature]
        kind = aliases.get(raw_kind, raw_kind)
        if kind not in allowed and any(key in raw for key in ("heading", "title", "paragraphs", "body", "content")):
            heading = str(raw.get("heading", raw.get("title", ""))).strip()
            if heading:
                blocks.append({"type": "heading", "text": heading[:500]})
            raw_text = raw.get("paragraphs", raw.get("body", raw.get("content", raw.get("text", ""))))
            blocks.extend({"type": "paragraph", "text": part[:5000]} for part in extract_text(raw_text)[:20])
            continue
        if kind not in allowed:
            continue
        if kind == "table":
            headers = [str(cell).strip()[:100] for cell in raw.get("headers", [])[:6]] if isinstance(raw.get("headers"), list) else []
            rows = [[str(cell).strip()[:160] for cell in row[:6]] for row in raw.get("rows", [])[:12] if isinstance(row, list)] if isinstance(raw.get("rows"), list) else []
            if headers and rows:
                blocks.append({"type": kind, "headers": headers, "rows": rows})
        elif kind == "divider":
            # Naver's long thin horizontal line is exposed as line1 in the
            # user's inspected editor menu and renders with se-l-line1.
            blocks.append({"type": "divider", "style": "line1"})
        elif kind in {"image", "place"}:
            text = str(raw.get("prompt" if kind == "image" else "query", "")).strip()[:500]
            if text:
                block = {"type": kind, "prompt" if kind == "image" else "query": text}
                role = str(raw.get("role", "")).strip()[:40]
                if role:
                    block["role"] = role
                blocks.append(block)
        else:
            texts = block_text(raw)
            for text in texts[:20]:
                if text:
                    if kind == "quote":
                        quote_style = str(raw.get("style", raw.get("quote_style", "vertical"))).strip().lower()
                        quote_styles = {"default", "quotation_line", "quotation_bubble", "quotation_underline", "quotation_postit", "quotation_corner"}
                        quote_style = {"vertical":"quotation_line", "line":"quotation_line", "bubble":"quotation_bubble", "postit":"quotation_postit", "frame":"quotation_corner", "underline":"quotation_underline"}.get(quote_style, quote_style)
                        block = {"type": kind, "text": text[:5000], "style": quote_style if quote_style in quote_styles else "quotation_line"}
                        role = str(raw.get("role", "")).strip().upper()
                        if role in {"SECTION", "POINT", "TIP", "CAUTION", "OPINION", "SUMMARY"}:
                            block["role"] = role
                        blocks.append(block)
                    else:
                        blocks.append({"type": kind, "text": text[:5000]})
    if not any(block["type"] in {"paragraph", "heading", "quote"} and str(block.get("text", "")).strip() for block in blocks):
        fallback_parts = []
        for key in ("introduction", "intro", "본문", "content", "body", "conclusion", "article_body", "post_content", "body_html", "text", "paragraphs", "sections"):
            part = payload.get(key)
            if isinstance(part, (str, list, dict)):
                fallback_parts.extend(extract_text(part))
        if fallback_parts:
            blocks.extend({"type": "paragraph", "text": part[:5000]} for part in fallback_parts[:30])
        else:
            keys = ", ".join(str(key)[:30] for key in list(payload)[:12]) or "없음"
            block_shape = ", ".join(sorted({
                f"{str(item.get('type', item.get('kind', '미지정')))}[{','.join(key for key in ('text','content','body','paragraph','paragraphs','value','nodes','children','title','heading') if key in item)}]"
                for item in value if isinstance(item, dict)
            })) if isinstance(value, list) else type(value).__name__
            raise ValueError(f"AI 응답에서 본문을 찾지 못했어요 (응답 항목: {keys}; 블록 형식: {block_shape or '없음'}). 다시 생성해 주세요.")
    type_features = {"paragraph": "text", "heading": "heading", "quote": "quotation", "divider": "divider", "table": "table", "image": "photo", "place": "place"}
    for block in blocks:
        block["editor_feature"] = type_features[block["type"]]
    return blocks


def _text_from_blocks(blocks: list[dict]) -> str:
    lines = []
    for block in blocks:
        if block["type"] == "table":
            lines.extend([" | ".join(block["headers"]), *[" | ".join(row) for row in block["rows"]]])
        elif block["type"] in {"image", "divider"}:
            continue
        elif block["type"] == "place":
            lines.append(f"장소: {block['query']}")
        else:
            lines.append(block["text"])
    return "\n\n".join(lines).strip()


def _body_character_count(blocks: list[dict]) -> int:
    """Count visible body characters, including spaces but excluding title and images."""
    return len(_text_from_blocks(blocks))


def _ensure_quote_and_divider(blocks: list[dict]) -> list[dict]:
    """Normalize Naver block metadata without inventing editorial content."""
    for block in blocks:
        if block.get("type") == "divider":
            block["style"] = "line1"
        if block.get("type") == "quote":
            valid_styles = {"default", "quotation_line", "quotation_bubble", "quotation_underline", "quotation_postit", "quotation_corner"}
            if block.get("style") not in valid_styles:
                block["style"] = "quotation_line"
    return blocks


def _polish_editor_blocks(blocks: list[dict], settings: dict, plan: dict | None = None) -> list[dict]:
    """Keep a useful pull quote and prevent headings or rules from dominating prose."""
    plan = plan if isinstance(plan, dict) else {}
    blocks = _ensure_quote_and_divider([dict(block) for block in blocks])
    if not settings.get("add_subheading"):
        blocks = [block for block in blocks if block.get("type") != "heading"]

    planned_quotes = plan.get("pull_quotes", [])
    planned_quotes = [item for item in planned_quotes if isinstance(item, dict)] if isinstance(planned_quotes, list) else []
    valid_quote_roles = {"SECTION", "POINT", "TIP", "CAUTION", "OPINION", "SUMMARY"}
    for planned_quote in planned_quotes:
        planned_text = re.sub(r"\s+", " ", str(planned_quote.get("text", "")).strip())
        if not planned_text:
            continue
        role = str(planned_quote.get("role", "POINT")).upper()
        role = role if role in valid_quote_roles else "POINT"
        existing_quote = next(
            (block for block in blocks if block.get("type") == "quote" and re.sub(r"\s+", " ", str(block.get("text", "")).strip()) == planned_text),
            None,
        )
        if existing_quote:
            existing_quote["role"] = role
            continue
        for block_index, block in enumerate(blocks):
            if block.get("type") != "paragraph":
                continue
            original = str(block.get("text", ""))
            normalized = re.sub(r"\s+", " ", original)
            if planned_text not in normalized:
                continue
            actual_text = next((part.strip() for part in re.split(r"(?<=[.!?。！？])\s*", original) if re.sub(r"\s+", " ", part.strip()) == planned_text), "")
            if not actual_text:
                continue
            quote = {"type": "quote", "editor_feature": "quotation", "text": actual_text, "role": role, "style": "quotation_line"}
            before, after = original.split(actual_text, 1)
            replacement = []
            if before.strip():
                replacement.append({**block, "text": before.strip()})
            replacement.append(quote)
            if after.strip():
                replacement.append({**block, "text": after.strip()})
            blocks[block_index:block_index + 1] = replacement
            break
    for quote in [block for block in blocks if block.get("type") == "quote"]:
        quote_text = str(quote.get("text", "")).strip()
        if not quote_text:
            continue
        for paragraph in blocks:
            if paragraph.get("type") != "paragraph" or quote_text not in paragraph.get("text", ""):
                continue
            paragraph["text"] = re.sub(r"\s+", " ", paragraph["text"].replace(quote_text, "", 1)).strip()
    blocks = [block for block in blocks if block.get("type") != "paragraph" or block.get("text", "").strip()]

    if not any(block.get("type") == "quote" for block in blocks):
        candidates = []
        paragraph_indices = [i for i, block in enumerate(blocks) if block.get("type") == "paragraph"]
        last_paragraph = paragraph_indices[-1] if paragraph_indices else -1
        for block_index, block in enumerate(blocks):
            if block.get("type") != "paragraph":
                continue
            for sentence in re.split(r"(?<=[.!?])\s+", block.get("text", "")):
                sentence = sentence.strip()
                if not 15 <= len(sentence) <= 100 or "http" in sentence:
                    continue
                decision_detail = bool(re.search(r"무료|유료|대기|주차|예약|가격|제한|불가|비용|달라|주의", sentence))
                specific = bool(re.search(r"\d|운영|확인|가능|방문|선택", sentence))
                score = (4 if decision_detail else 0) + (2 if specific else 0) + (1 if 35 <= len(sentence) <= 90 else 0) - (2 if block_index == 0 else 0)
                if block_index == last_paragraph:
                    score -= 3
                if re.search(r"공식 안내|확인할 수 있다|확인하는 것이 좋다|변경될 수", sentence):
                    score -= 6
                candidates.append((score, block_index, sentence))
        if not candidates:
            candidates = [
                (0, i, block["text"].strip())
                for i, block in enumerate(blocks)
                if block.get("type") == "paragraph" and 10 <= len(block.get("text", "").strip()) <= 120
            ]
        if candidates:
            _, block_index, sentence = max(candidates)
            paragraph = blocks[block_index]
            remainder = re.sub(r"\s+", " ", paragraph["text"].replace(sentence, "", 1)).strip()
            fallback_quote = planned_quotes[0] if planned_quotes else {}
            role = str(fallback_quote.get("role", "POINT")).upper()
            if role not in valid_quote_roles:
                role = "POINT"
            quote = {"type": "quote", "editor_feature": "quotation", "text": sentence, "role": role, "style": "quotation_line"}
            if remainder:
                paragraph["text"] = remainder
                blocks.insert(block_index + 1, quote)
            else:
                blocks[block_index] = quote

    blocks = [block for block in blocks if block.get("type") != "divider"]
    divider_heading = str(plan.get("divider_before_heading", "")).strip()
    if divider_heading:
        heading_index = next((i for i, block in enumerate(blocks) if block.get("type") == "heading" and block.get("text", "").strip() == divider_heading), None)
        if heading_index is not None:
            blocks.insert(heading_index, {"type": "divider", "editor_feature": "divider", "style": "line1"})
    while blocks and blocks[0].get("type") == "divider":
        blocks.pop(0)
    while blocks and blocks[-1].get("type") == "divider":
        blocks.pop()
    # The opening should begin with the article itself, not a table-of-contents
    # label such as "기본 정보". Headings are useful after the first prose block.
    while blocks and blocks[0].get("type") in {"heading", "divider"}:
        blocks.pop(0)
    return blocks


def _research_topic(client: object, keyword: str) -> tuple[str, list[dict]]:
    """Collect current, attributable facts before asking the writer to draft."""
    response = client.responses.create(
        model="gpt-4.1",
        tools=[{"type": "web_search"}],
        tool_choice="required",
        include=["web_search_call.action.sources"],
        input=(
            f"한국어 블로그 글을 위한 사실 조사입니다. 검색어: {keyword}\n"
            "같은 이름의 인물·상품·행사를 혼동하지 마세요. 공식 발표, 주최 측, 제조사, 공공기관의 "
            "사실이 적힌 상세 페이지를 우선 확인하세요. 기관 첫 화면, 검색 결과 페이지, 개인 블로그, "
            "다른 글에서 공식 자료를 재인용한 페이지만 있으면 사실을 확인한 것으로 취급하지 마세요. "
            "확인된 사실과 날짜를 한 줄에 하나씩 쓰고 각 줄 끝에 직접 확인한 상세 페이지 URL을 인용하세요. "
            "추측, 광고 문구, 개인 사용 경험, 확인되지 않은 가격·맛·일정·인기·인물 관계는 제외하세요. "
            "자료가 부족하거나 검색 결과가 서로 충돌하면 그 상태를 명시하세요. 비교 주제라면 각 항목을 같은 기준으로 확인하고, 핵심 판단을 뒷받침하는 출처를 우선하세요."
        ),
    )
    notes = str(response.output_text or "").strip()[:8000]
    sources: list[dict] = []
    seen: set[str] = set()

    def source_key(url: str) -> tuple:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        query = tuple(sorted((key, value) for key, value in parse_qsl(parsed.query) if not key.startswith("utm_")))
        return host, parsed.path.rstrip("/"), query

    searched_urls = {
        source_key(str(getattr(source, "url", "") or ""))
        for item in response.output if getattr(item, "type", "") == "web_search_call"
        for source in (getattr(getattr(item, "action", None), "sources", None) or [])
        if getattr(source, "type", "") == "url"
    }
    for item in response.output:
        if getattr(item, "type", "") != "message":
            continue
        for content in getattr(item, "content", []):
            for annotation in getattr(content, "annotations", []):
                if getattr(annotation, "type", "") != "url_citation":
                    continue
                url = str(getattr(annotation, "url", "") or "").strip()
                parsed = urlparse(url)
                if parsed.scheme not in {"http", "https"} or url in seen:
                    continue
                if source_key(url) not in searched_urls:
                    continue
                if parsed.path.lower().rstrip("/") in {"", "/index.html", "/index.php", "/index.jsp", "/main", "/main.do"}:
                    continue
                if parsed.hostname and (parsed.hostname.endswith("tistory.com") or parsed.hostname == "blog.naver.com"):
                    continue
                seen.add(url)
                sources.append({"title": str(getattr(annotation, "title", "") or url)[:160], "url": url})
    # Discard research statements whose cited page did not pass the source check.
    accepted_urls = {source["url"] for source in sources}
    notes = "\n".join(
        line.strip() for line in notes.splitlines()
        if any(url in line for url in accepted_urls)
    )[:8000]
    if not notes or not sources:
        raise RuntimeError("주제에 관해 출처가 확인된 정보를 찾지 못했어요. 더 구체적인 키워드로 다시 생성해 주세요.")
    return notes, sources[:8]


def _plan_article(client: object, keyword: str, research_notes: str, sources: list[dict], settings: dict) -> dict:
    """Choose the reader's question, editorial angle and block jobs before drafting."""
    brief = {
        "topic": keyword,
        "style": settings.get("style", "auto"),
    }
    prompt = f"""한국어 블로그 글의 편집 기획자입니다. 아직 글을 쓰지 말고, 먼저 무엇을 말할 가치가 있는지 정하세요.
주제와 작성 방식: {json.dumps(brief, ensure_ascii=False)}
확인된 사실: {research_notes}
출처: {json.dumps(sources, ensure_ascii=False)}

다음 구조의 JSON 객체만 반환하세요.
{{
  "reader_question": "검색자가 실제로 알고 싶어 할 질문 하나",
  "angle": "이 글이 다룰 관점 한 문장",
  "judgment": "자료에서 합리적으로 내릴 수 있는 중심 판단 한 문장",
  "opening_type": "situation|problem|discovery|judgment|context|fact 중 하나",
  "opening_basis": "첫 문장에 쓸 확인된 사실이나 독자가 처한 구체적인 상황",
  "article_shape": "comparison|decision_guide|explanation|review|context|other 중 주제에 맞는 형태",
  "selected_points": [{{"fact":"확인 자료에서 뽑은 사실", "reader_value":"왜 중요한지", "priority":1, "space":"more|less"}}],
  "comparison_axes": [{{"criterion":"비교 기준", "differences":"각 선택지에서 확인된 차이", "reader_impact":"그 차이가 독자의 선택에 미치는 영향"}}],
  "sections": [{{"heading":"필요한 경우의 구체적인 소제목", "purpose":"이 부분에서 답할 질문", "selected_facts":["사용할 확인 사실"], "reader_takeaway":"독자가 이 부분에서 얻을 판단"}}],
  "headings": ["내용을 구체적으로 예고하는 소제목"],
  "pull_quotes": [{{"text":"본문에서 그대로 사용할 짧고 구체적인 문장", "role":"TIP|POINT|CAUTION|OPINION|SECTION", "reason":"이 문장을 시각적으로 분리하면 읽기 쉬워지는 이유"}}],
  "divider_before_heading": "큰 주제가 바뀌는 소제목 하나 또는 빈 문자열",
  "ending_basis": "요약이 아닌 마지막 판단의 근거",
  "image_roles": [{{"section":"연결할 소제목 또는 도입", "role":"본문에서 설명하지 않을 시각 자료의 역할"}}]
}}

규칙:
- 독자의 질문에 답하는 데 필요한 사실을 충분히 고르세요. 정보 개수를 맞추거나 근거를 억지로 늘리지 말고, 중요도가 높은 사실에 더 많은 설명 공간을 배정하세요.
- 비교 주제라면 각 선택지를 따로 소개하는 데 그치지 말고, 검색자가 실제로 비교할 공통 기준을 찾아 같은 기준끼리 대조하세요. 수치나 기능의 차이뿐 아니라 그 차이가 사용·구매 판단에 미치는 영향과 적용 조건을 적으세요. 확인되지 않은 항목은 추정하지 마세요.
- 어떤 글 형태가 적절한지 주제와 검색 의도로 결정하세요. 항목 나열보다 설명이 필요한 곳은 문장으로 풀고, 한눈에 대조하거나 확인할 내용은 목록·표 등 읽기 쉬운 형식을 선택하세요.
- 제공된 정보가 충분하면 핵심을 한두 문단으로 끝내지 말고, 독자의 판단에 필요한 근거와 조건까지 설명하세요. 자료가 적으면 짧게 쓰되 그 한계를 분명히 하세요.
- 방문·구매·사용 경험을 지어내지 마세요. 독자의 문제, 선택 기준, 확인된 사실 중 주제에 맞는 구체적인 내용으로 시작하세요. 주제를 사전처럼 정의하거나 글의 구성을 예고하는 도입은 고르지 마세요.
- 중심 판단은 선택한 사실의 이유를 담아야 합니다. 근거가 없는 칭찬이나 감상은 계획하지 마세요.
- 인용구 후보를 형식적으로 하나만 고르지 마세요. 글의 핵심 정보, 판단 기준, 주의점, 독자가 기억할 결론 중 시각적으로 분리할 가치가 있는 문장을 글의 여러 흐름에서 찾으세요. 정보 덩어리와 글의 리듬에 맞춰 충분히 계획하고, 후보마다 다른 역할과 근거를 두세요. 문단마다 붙이거나 인용구를 연달아 두지는 마세요. 실제 인물의 말이 아닌 문장을 발언처럼 표시하지 마세요.
- 구분선은 독립적인 큰 주제가 바뀔 때만 하나 지정하세요. 장식용으로 넣지 마세요.
- 이미지는 AI 삽화입니다. 실제 장소·상품·인물 사진인 것처럼 쓰지 말고, 본문에 적힌 시각 정보를 되풀이하지 않도록 역할만 계획하세요.
"""
    response = client.chat.completions.create(
        model="gpt-4.1",
        messages=[
            {"role": "system", "content": "자료에서 글의 중심을 고르는 편집 기획자입니다. JSON 객체만 반환하세요."},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.25,
        max_tokens=2500,
    )
    try:
        plan = json.loads(response.choices[0].message.content or "{}")
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("글의 질문과 중심 내용을 정리하지 못했어요. 다시 생성해 주세요.") from exc
    if not isinstance(plan, dict) or not str(plan.get("reader_question", "")).strip() or not str(plan.get("angle", "")).strip():
        raise RuntimeError("글의 질문과 중심 내용을 정리하지 못했어요. 다시 생성해 주세요.")
    plan["selected_points"] = [item for item in plan.get("selected_points", []) if isinstance(item, dict)][:12] if isinstance(plan.get("selected_points"), list) else []
    plan["sections"] = [item for item in plan.get("sections", []) if isinstance(item, dict)][:12] if isinstance(plan.get("sections"), list) else []
    plan["comparison_axes"] = [item for item in plan.get("comparison_axes", []) if isinstance(item, dict)][:12] if isinstance(plan.get("comparison_axes"), list) else []
    plan["headings"] = [str(item).strip()[:100] for item in plan.get("headings", []) if str(item).strip()][:12] if isinstance(plan.get("headings"), list) else []
    plan["pull_quotes"] = [item for item in plan.get("pull_quotes", []) if isinstance(item, dict)] if isinstance(plan.get("pull_quotes"), list) else []
    plan["image_roles"] = [item for item in plan.get("image_roles", []) if isinstance(item, dict)][:5] if isinstance(plan.get("image_roles"), list) else []
    return plan


def _quality_issues(blocks: list[dict]) -> list[str]:
    """Flag only objectively repeated body paragraphs; style is reviewed by the editor prompt."""
    paragraphs = [
        re.sub(r"\s+", "", str(block.get("text", "")))
        for block in blocks
        if block.get("type") == "paragraph" and str(block.get("text", "")).strip()
    ]
    return ["문단 반복"] if len(paragraphs) != len(set(paragraphs)) else []


def generate_blog_draft(api_key: str, settings: dict, account_id: str,
                         access_token: str | None = None) -> dict:
    """Return a validated structured draft and locally persisted GPT Image assets."""
    from core.llm import create_openai_client

    keywords = [str(word).strip()[:40] for word in settings.get("keywords", []) if str(word).strip()][:10]
    if not keywords:
        raise ValueError("주제나 키워드를 한 개 이상 입력해 주세요.")
    style_names = {"auto": "AI 주제 맞춤형", "informative": "정보형", "review": "후기형", "expert": "전문가형"}
    style_instructions = {
        "auto": "주제, 검색자의 의도, 확인된 자료를 보고 글의 형식과 관점을 직접 정하세요. 뉴스 기사나 보도자료처럼 중립적인 사실을 순서대로 나열하지 말고, 한 독자가 실제로 궁금해할 질문에서 시작해 필요한 정보에 분량을 쓰고 자료에서 가능한 판단을 분명하게 내리세요. 1인칭 방문·구매·사용 경험을 만들지 마세요.",
        "informative": "정보형입니다. 독자가 해결하려는 질문을 파악하고 답과 판단 근거를 분명하게 전달하세요. 주제에 필요한 맥락, 조건, 예외만 골라 설명하고 항목을 억지로 채우지 마세요.",
        "review": "실제 사용·구매·방문 경험을 지어내지 마세요. 확인된 정보에서 독자가 실제로 궁금해할 한 가지를 골라 판단하세요. 상품은 구매, 행사는 방문, 인물 이슈는 사실관계처럼 주제에 맞는 질문을 다루세요. 장단점을 억지로 대칭시키지 마세요.",
        "expert": "전문가형입니다. 원인과 작동 방식, 판단 기준을 정확하고 이해하기 쉽게 설명하세요. 복잡한 내용은 필요한 만큼 풀어 쓰고, 조건이나 예외가 결론을 바꾸는 경우 함께 밝히세요. 입력에 없는 자격·경력·전문가 권위를 주장하지 마세요.",
    }
    style = style_names.get(settings.get("style"), "AI 주제 맞춤형")
    include = [label for key, label in (("add_subheading", "소제목"), ("add_hashtags", "해시태그"), ("add_links", "관련 링크"), ("add_cta", "마무리 안내 문구")) if settings.get(key)]
    image_enabled = bool(settings.get("images_enabled")) and settings.get("image_source") == "ai"
    auto_count = bool(settings.get("image_count_auto"))
    image_count_instruction = (
        ("본문의 정보 밀도에 따라 1~5개 중 필요한 개수를 결정" if auto_count else f"정확히 {max(0, min(5, int(settings.get('image_count', 0))))}개")
        if image_enabled else "0개"
    )
    client = create_openai_client(api_key, access_token, timeout=120.0, max_retries=1)
    research_notes, sources = _research_topic(client, keywords[0])
    editorial_plan = _plan_article(client, keywords[0], research_notes, sources, settings)
    common_guidance = f"""당신은 한 명의 독자에게 말하듯 글을 쓰는 네이버 블로그 작성자입니다. 보도자료나 뉴스 기사의 어조로 쓰지 마세요.
주제/키워드: {json.dumps(keywords, ensure_ascii=False)}
글 유형: {style}. 작성 관점: {style_instructions.get(settings.get('style'), style_instructions['informative'])}
독자에게 직접 설명하는 자연스러운 존댓말을 사용하세요. 문장 종결과 문단 구조를 반복하지 말고, 확인된 사실이 독자의 일정·비용·선택에 어떤 의미인지 풀어 쓰세요. 입력되지 않은 개인 경험을 암시하는 표현은 사용하지 마세요.
본문은 제목과 이미지 설명을 제외하고 공백 포함 {MINIMUM_BODY_CHARACTERS:,}자 이상이어야 합니다. 이 기준은 반드시 지키되 같은 말을 반복하거나 중요하지 않은 상식으로 분량을 채우지 마세요. 확인한 자료와 합리적으로 설명할 수 있는 맥락을 충분히 풀어 쓰고, 조건·차이·독자에게 미치는 영향을 설명하세요. 근거가 없는 사실이나 개인 경험을 만들어 분량을 채우지 마세요. 포함 설정: {', '.join(include) or '본문만'}.
확인한 자료: {research_notes}
출처: {json.dumps(sources, ensure_ascii=False)}
사전 편집 기획: {json.dumps(editorial_plan, ensure_ascii=False)}
확인한 자료를 벗어나는 개인 경험, 대화, 통계, 가격, 주소, 사양, 맛, 인기도, 관계, 행사 일정을 지어내지 마세요. 출처가 충돌하면 단정하지 마세요. 자료의 맥락과 확인된 조건을 더 자세히 설명해 기준을 채우고, 사실을 새로 지어내지는 마세요.
목표는 사람이 읽고 실제 판단에 쓸 정보를 얻는 글입니다. 위 기획의 질문과 중심 판단을 유지하고, 중요한 사실에는 이유·조건·독자에게 생기는 차이를 붙이세요. 문단 길이와 문장 구조를 섞고 접속어를 억지로 넣지 마세요. 독자가 이미 아는 상식이나 근거 없는 평가를 늘리지 마세요.
비교 주제는 공통 기준을 중심으로 선택지들을 직접 대조하세요. 각 제품이나 대상을 따로 소개하는 데 그치지 말고, 기준별 차이와 그 차이가 실제 선택에 미치는 영향을 설명하세요. 서로 다른 세대나 지역별 사양처럼 비교를 바꾸는 조건은 먼저 확인하고 드러내세요. 비교 자료가 충분하면 읽기 쉬운 표나 목록을 활용해도 됩니다. 자료로 뒷받침되지 않는 항목은 추정해 채우지 마세요. 비교 외의 주제는 검색자의 의도에 맞는 흐름을 직접 고르세요.
구체적인 사실을 먼저 쓰고 그 사실 때문에 내린 판단을 뒤에 쓰세요. 판단 근거가 없다면 평가 문장을 삭제하세요. 검색 키워드는 자연스러운 위치에만 사용하고 반복하지 마세요. 정형적인 서론·결론, 항목을 채우기 위한 나열, 모든 문단의 긍정적인 마무리, 앞 내용 재요약, 주제를 바꾸어도 그대로 쓸 수 있는 문장을 피하세요.
제목과 첫 문장은 위 기획의 중심 질문과 도입 방식을 반영하세요. 독자의 문제·선택 기준·확인된 사실 중 주제에 맞는 내용으로 시작하세요. 행사나 상품을 기관 소개문처럼 정의하지 말고, 독자가 방문·구매·사용 여부를 판단하는 데 필요한 내용을 다루세요. 실제 체험을 한 것처럼 쓰지 마세요.
기획된 pull_quotes 중 본문에 실제로 사용한 가치 있는 문장은 각각 인용구 블록으로 시각화하세요. 글의 핵심 정보, 판단 기준, 주의점, 관점처럼 독자가 훑어볼 때 도움이 되는 부분을 충분히 활용하되, 개수나 비율을 미리 정하지 마세요. 각 인용구는 서로 다른 내용을 맡아야 합니다. 본문에 같은 문장을 반복하거나 인용구를 연달아 배치하지 말고 앞뒤 설명과 함께 읽히게 하세요. 장식용으로 문장을 떼어내거나 모든 문단에 기계적으로 붙이지 마세요. 실제 발언이 아닌 문장을 인물의 말처럼 표시하지 마세요.
소제목 설정이 꺼졌으면 소제목을 쓰지 마세요. 켜졌다면 내용 전환과 독자 탐색에 도움이 되는 곳에만 쓰세요. 소제목 수를 미리 정하지 말고, 첫 문장은 소제목 없이 바로 시작하세요. 같은 질문에 답하는 내용은 한 흐름으로 묶으세요.
소제목은 해당 부분에서 답할 질문이나 확인된 내용을 드러내고, 같은 글 안에서 모양을 기계적으로 반복하지 마세요.
구분선은 주제가 크게 바뀌어 독자가 잠시 끊어 읽을 필요가 있을 때만 최대 1개 사용하세요. 글 길이와 관계없이 장식용으로 넣지 마세요. 사용할 때는 style=line1을 지정하세요.
이미지는 글의 설명을 보완하는 역할과 위치를 판단하세요. 이미지 수는 {image_count_instruction}입니다. 이미지는 AI 삽화이므로 실제 장소·상품·인물의 근거처럼 묘사하지 마세요. 그림이 보여주는 내용을 본문에서 반복하지 말고, 본문에는 이미지로 알 수 없는 조건과 판단 근거를 쓰세요. 이미지 역할과 장면은 위 기획과 연결하고 고정 순서를 두지 마세요. 주제에서 알 수 없는 실물 특징, 글자, 로고, 워터마크를 만들지 마세요. 이미지가 꺼져 있으면 이미지 블록을 만들지 마세요. 표는 여러 항목을 직접 대조하는 데 도움이 될 때 사용하고, 각 행은 확인된 정보와 짧은 독자 관점을 담으세요.
마지막 문단은 전체 내용을 요약하지 말고 기획한 판단을 근거와 함께 짧게 마무리하세요. 재방문·재구매 의사를 지어내지 마세요.
해시태그와 CTA는 해당 설정이 있을 때만 포함하세요. 문장에 마크다운 기호를 쓰지 마세요."""

    writer_prompt = common_guidance + f"""
확인한 자료의 사실 중 독자의 선택이나 이해에 도움이 되는 것부터 골라 쓰세요. 글에 필요한 정보만 남기고 자료를 모두 나열하지 마세요. 현재 이용 방법을 묻는 글에 과거 제도나 주변 역사를 덧붙이는 식으로 분량을 채우지 마세요.
완성된 글을 작성해 JSON 객체만 반환하세요. 최상위 키는 title, image_count, blocks입니다. image_count는 정수입니다. 모든 블록에는 type과 editor_feature를 지정하세요: paragraph/text, heading/heading, quote/quotation, divider/divider, table/table, image/photo. blocks는 본문 순서입니다.
표는 여러 항목을 같은 기준으로 비교할 때 읽기 쉬워지는 경우에만 headers와 rows로 구성하세요. 인용구에는 앞뒤 문단과 중복되지 않는 한 문장 text, 맥락에 맞는 role, 네이버 style을 지정하세요. 구분선은 큰 주제가 바뀔 때만 사용하고 style=line1로 지정하세요. 이미지 블록은 prompt와 role을 포함하세요. 마지막 문단은 새 정보나 실제 판단으로 끝내고 앞 내용을 다시 요약하지 마세요.
출력 전 확인: 학교 과제 같은 목차, 비슷한 길이의 문단 반복, 매 문단의 긍정적 마무리, 반복 요약, 근거 없는 칭찬, 주제를 바꾸어도 통하는 문장이 있으면 그 부분을 고쳐 쓰세요."""
    response = client.chat.completions.create(
        model="gpt-4.1",
        messages=[{"role": "system", "content": "확인된 자료에 충실하고 자연스러운 한국어 블로그 글을 작성합니다. JSON 객체만 반환하세요."}, {"role": "user", "content": writer_prompt}],
        response_format={"type": "json_object"},
        temperature=0.7,
        max_tokens=6500,
    )
    try:
        payload = json.loads(response.choices[0].message.content or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("AI가 포스팅 응답을 JSON으로 반환하지 않았어요. 다시 생성해 주세요.") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("AI 포스팅 응답의 최상위 형식이 객체가 아니에요. 다시 생성해 주세요.")
    if isinstance(payload.get("post"), dict):
        payload = payload["post"]
    try:
        blocks = _clean_blocks(payload.get("blocks"), payload)
    except ValueError:
        # A malformed/empty blocks payload is a model response failure, not a
        # reason to leave the user with a dead end. Regenerate once under a
        # stricter schema contract, then normalize editor blocks.
        retry_prompt = writer_prompt + f"""

이전 응답의 blocks에서 본문 문장을 읽을 수 없었습니다. 같은 주제와 설정으로 글 전체를 새로 작성하세요.
각 paragraph, heading, quote 블록은 반드시 비어 있지 않은 한국어 문장 문자열을 text 키에 직접 넣으세요. 문장을 중첩 객체나 배열로 감싸지 마세요.
글의 리듬과 가독성을 높이는 데 필요한 인용구 블록을 적절히 배치하세요. 각 quote에는 type=quote, editor_feature=quotation, text, role, style을 넣으세요. 서로 다른 핵심 정보를 강조하고, 인용구끼리 연달아 놓거나 앞뒤 문단에서 같은 내용을 반복하지 마세요. 큰 주제 전환이 있을 때만 divider에 type=divider, editor_feature=divider, style=line1을 넣으세요.
반환은 title, image_count, blocks를 가진 JSON 객체만 허용합니다.
"""
        try:
            retry_response = client.chat.completions.create(
                model="gpt-4.1",
                messages=[{"role": "system", "content": "확인된 자료에 충실하고 자연스러운 한국어 블로그 글을 작성합니다. JSON 객체만 반환하세요."}, {"role": "user", "content": retry_prompt}],
                response_format={"type": "json_object"},
                temperature=0.5,
                max_tokens=6500,
            )
            retry_payload = json.loads(retry_response.choices[0].message.content or "{}")
            if isinstance(retry_payload, dict) and isinstance(retry_payload.get("post"), dict):
                retry_payload = retry_payload["post"]
            if not isinstance(retry_payload, dict):
                raise ValueError("JSON 객체가 아닙니다")
            retry_blocks = _clean_blocks(retry_payload.get("blocks"), retry_payload)
            if retry_blocks:
                payload = retry_payload
                blocks = retry_blocks
            else:
                raise ValueError("본문 블록이 비어 있습니다")
        except Exception as retry_error:
            raise RuntimeError("AI가 본문 문장을 비운 응답을 두 번 반환해 생성을 멈췄어요. 잠시 후 다시 생성해 주세요.") from retry_error
    blocks = _ensure_quote_and_divider(blocks)
    title = re.sub(r"\s+", " ", str(payload.get("title", "")).strip())[:150]
    if not title:
        raise ValueError("AI가 제목을 만들지 못했어요.")

    editor_feedback = _quality_issues(blocks)
    editor_review_passed = False
    for edit_round in range(3):
        revision = client.chat.completions.create(
            model="gpt-4.1",
            messages=[
                {"role": "system", "content": "자료와 초안을 대조해 근거 없는 주장을 바로잡고, 사람이 읽기 자연스럽고 구체적인 한국어 블로그 글로 다시 씁니다. JSON 객체만 반환하세요."},
                {"role": "user", "content": (
                    f"확인한 자료: {research_notes}\n출처: {json.dumps(sources, ensure_ascii=False)}\n"
                    f"사전 편집 기획: {json.dumps(editorial_plan, ensure_ascii=False)}\n"
                    f"현재 초안: {json.dumps({'title': title, 'blocks': blocks}, ensure_ascii=False)}\n"
                    f"앞선 검수 의견: {json.dumps(editor_feedback, ensure_ascii=False)}\n수정 차수: {edit_round + 1}/3\n"
                    "초안의 구체적 주장마다 자료로 뒷받침되는지 확인하세요. 자료에 없는 맛·가격·일정·인기도·개인 경험·인물 관계는 삭제하세요. "
                    f"본문은 제목과 이미지 설명을 제외하고 공백 포함 {MINIMUM_BODY_CHARACTERS:,}자 이상이어야 합니다. 반복과 군더더기 없이 확인된 정보의 조건, 맥락, 비교 기준과 독자에게 미치는 영향을 충분히 설명하세요. "
                    "독자의 핵심 질문에 답을 더하지 않는 과거 제도, 배경, 사소한 수치는 넣지 마세요. 자료에 없는 사실이나 개인 경험을 만들어 분량을 늘리지 마세요. "
                    "문장을 고칠 때 글을 짧게 줄여 기준에 미달시키지 마세요. 문단 길이를 일부러 맞추지 말고 구체적인 정보 뒤에만 판단을 두세요. "
                    "사전 편집 기획의 독자 질문, 중심 판단, 중요 사실 우선순위를 따르세요. 방문·구매·사용 장면을 지어내지 마세요. "
                    "기획한 인용구 문장을 살리고, 사진용 AI 삽화가 실제 장소·상품의 증거처럼 보이게 쓰지 마세요. "
                    "글을 다시 읽고 다음 기준에 어긋나는 부분을 이번 응답에서 반드시 고치세요: 뉴스 보도처럼 사실을 나열하는 도입, 문단 길이와 문장 구조의 반복, 매 단락의 긍정적인 마무리, 이미 한 말의 재요약, 구체적 근거 없는 평가, 주제를 바꿔도 통하는 문장. "
                    "인용구는 핵심 정보·주의점·판단·섹션 진입점 중 독자가 훑어볼 때 도움이 되는 곳에 활용하되 같은 내용을 반복하거나 연달아 배치하지 마세요. 소제목과 구분선은 글의 흐름에 필요한 만큼만 사용하세요. "
                    "최종 초안을 자체 검수하세요. 수정이 더 필요하면 quality_review.passed를 false로 하고 남은 문제를 issues 배열에 간단히 적으세요. 통과하면 passed를 true로 하고 issues는 빈 배열로 두세요. 문제를 발견해도 오류나 실패로 반환하지 말고 수정한 글을 반환하세요. "
                    "title, image_count, blocks, quality_review를 가진 JSON 객체로 반환하세요. 각 블록의 type, editor_feature와 필요한 image prompt를 유지하세요."
                )},
            ],
            response_format={"type": "json_object"},
            temperature=0.4 if edit_round == 0 else 0.3,
            max_tokens=6500,
        )
        try:
            revised = json.loads(revision.choices[0].message.content or "{}")
            if isinstance(revised, dict) and isinstance(revised.get("post"), dict):
                revised = revised["post"]
            if not isinstance(revised, dict):
                raise ValueError("검토 응답이 JSON 객체가 아닙니다")
            revised_blocks = _clean_blocks(revised.get("blocks"), revised)
        except (ValueError, TypeError, KeyError) as exc:
            raise RuntimeError("초안 편집 응답을 읽지 못했어요. 다시 생성해 주세요.") from exc
        if not revised_blocks:
            raise RuntimeError("초안 편집 결과의 본문이 비어 있어요. 다시 생성해 주세요.")
        title = re.sub(r"\s+", " ", str(revised.get("title") or title).strip())[:150]
        blocks = _polish_editor_blocks(revised_blocks, settings, editorial_plan)
        payload = revised
        review = revised.get("quality_review") if isinstance(revised.get("quality_review"), dict) else {}
        editor_feedback = [str(item)[:240] for item in review.get("issues", []) if str(item).strip()] if isinstance(review.get("issues"), list) else []
        editor_feedback.extend(_quality_issues(blocks))
        editor_review_passed = review.get("passed") is True and not _quality_issues(blocks)
        if editor_review_passed:
            break

    # The writer and copy editor can both shorten drafts. Enforce the requested
    # minimum after editorial cleanup, expanding only with supported detail.
    for _ in range(2):
        body_characters = _body_character_count(blocks)
        if body_characters >= MINIMUM_BODY_CHARACTERS:
            break
        expansion_prompt = (
            f"확인된 자료: {research_notes}\n출처: {json.dumps(sources, ensure_ascii=False)}\n"
            f"사전 편집 기획: {json.dumps(editorial_plan, ensure_ascii=False)}\n"
            f"현재 초안 본문 글자 수: 공백 포함 {body_characters:,}자. 필요한 최소 분량: {MINIMUM_BODY_CHARACTERS:,}자.\n"
            f"현재 초안: {json.dumps({'title': title, 'blocks': blocks}, ensure_ascii=False)}\n\n"
            "현재 초안을 독자에게 실제로 도움이 되는 정보 밀도로 확장하세요. 단순히 문장을 길게 바꾸거나 같은 내용을 반복하지 마세요. "
            "확인된 사실의 조건과 맥락, 비교 기준별 차이, 선택에 미치는 영향, 독자가 놓치기 쉬운 예외를 자료 범위 안에서 설명하세요. "
            "자료에 없는 경험·수치·가격·일정·사양을 만들지 마세요. 근거가 약한 내용은 추가하지 말고 확인된 정보의 의미를 구체적으로 풀어 쓰세요. "
            "기존 글의 자연스러운 도입과 중심 판단을 유지하고, 문단 길이를 일부러 맞추지 마세요. 인용구는 서로 다른 핵심 정보나 판단을 강조할 때 활용하고 연달아 배치하지 마세요. "
            f"본문은 제목과 이미지 설명을 제외하고 공백 포함 {MINIMUM_BODY_CHARACTERS:,}자 이상으로 작성하세요. 제목, 인용구, 소제목, 표의 실제 텍스트는 본문 글자 수에 포함됩니다. "
            "title, image_count, blocks가 있는 JSON 객체로 반환하세요. 기존 이미지 블록과 필요한 블록 메타데이터를 유지하세요."
        )
        expansion = client.chat.completions.create(
            model="gpt-4.1",
            messages=[
                {"role": "system", "content": "확인된 자료와 초안을 바탕으로 정보 가치가 있는 한국어 블로그 글을 충분히 설명합니다. JSON 객체만 반환하세요."},
                {"role": "user", "content": expansion_prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.45,
            max_tokens=6500,
        )
        try:
            expanded = json.loads(expansion.choices[0].message.content or "{}")
            if isinstance(expanded, dict) and isinstance(expanded.get("post"), dict):
                expanded = expanded["post"]
            expanded_blocks = _clean_blocks(expanded.get("blocks"), expanded) if isinstance(expanded, dict) else []
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise RuntimeError("본문을 3,000자 이상으로 확장하지 못했어요. 다시 생성해 주세요.") from exc
        if not expanded_blocks:
            raise RuntimeError("본문을 3,000자 이상으로 확장하지 못했어요. 다시 생성해 주세요.")
        blocks = _polish_editor_blocks(_ensure_quote_and_divider(expanded_blocks), settings, editorial_plan)
    body_characters = _body_character_count(blocks)
    if body_characters < MINIMUM_BODY_CHARACTERS:
        raise RuntimeError(
            f"자료에 없는 내용을 보태지 않고는 본문을 3,000자까지 채우기 어려워 생성을 멈췄어요. 현재 {body_characters:,}자입니다. 주제를 더 구체적으로 입력하거나 참고 자료를 추가해 주세요."
        )

    post_id = uuid.uuid4().hex
    image_count = max(0, min(5, int(settings.get("image_count", 0))))
    if image_enabled and auto_count:
        try:
            image_count = int(payload.get("image_count", 0))
        except (TypeError, ValueError):
            image_count = 0
        if image_count <= 0:
            image_count = sum(block["type"] == "image" for block in blocks)
        if image_count <= 0:
            paragraph_count = sum(block["type"] in {"paragraph", "heading"} for block in blocks)
            image_count = max(1, min(5, (paragraph_count + 3) // 4))
        image_count = max(1, min(5, image_count))
    image_dir = _account_dir(account_id) / "blog_post_images" / post_id
    images: list[dict] = []
    if image_enabled:
        image_blocks = [block for block in blocks if block["type"] == "image"][:image_count]
        chosen_image_ids = {id(block) for block in image_blocks}
        blocks[:] = [block for block in blocks if block.get("type") != "image" or id(block) in chosen_image_ids]
        while len(image_blocks) < image_count:
            index = len(image_blocks)
            image_specs = editorial_plan.get("image_roles", [])
            image_spec = image_specs[index] if isinstance(image_specs, list) and index < len(image_specs) and isinstance(image_specs[index], dict) else {}
            section_name = str(image_spec.get("section", "")).strip()
            insert_at = None
            if section_name:
                section_index = next((i for i, block in enumerate(blocks) if block.get("type") == "heading" and block.get("text", "").strip() == section_name), None)
                if section_index is not None:
                    insert_at = next((i + 1 for i in range(section_index + 1, len(blocks)) if blocks[i].get("type") == "paragraph"), section_index + 1)
            if insert_at is None:
                insert_at = min(len(blocks), max(1, round((len(blocks) + 1) * (index + 1) / (image_count + 1))))
            context = next((block.get("text", "") for block in reversed(blocks[:insert_at]) if block.get("type") == "paragraph"), ", ".join(keywords))
            image_role = str(image_spec.get("role", "")).strip() or "AI illustration supporting the selected section"
            generated_block = {"type": "image", "editor_feature": "photo", "role": image_role, "prompt": f"Create a realistic editorial photograph illustrating this Korean blog article. Topic: {', '.join(keywords)}. Article section: {context[:240]}. Image role: {image_role}. Distinct composition, natural light, no text, no logo, no watermark."}
            blocks.insert(insert_at, generated_block)
            image_blocks.append(generated_block)
        image_dir.mkdir(parents=True, exist_ok=True)
        try:
            for index, block in enumerate(image_blocks, start=1):
                result = client.images.generate(model="gpt-image-2", prompt=block["prompt"], n=1)
                encoded = result.data[0].b64_json
                if not encoded:
                    raise RuntimeError("이미지 생성 결과를 받지 못했어요.")
                path = image_dir / f"{index:02d}.png"
                path.write_bytes(base64.b64decode(encoded))
                image = {"id": f"image-{index}", "path": str(path), "alt": f"본문 이미지 {index}", "order": index}
                images.append(image)
                block["image_id"] = image["id"]
        except Exception:
            shutil.rmtree(image_dir, ignore_errors=True)
            raise
    elif not image_enabled:
        blocks[:] = [block for block in blocks if block.get("type") != "image"]

    type_features = {"paragraph": "text", "heading": "heading", "quote": "quotation", "divider": "divider", "table": "table", "image": "photo", "place": "place"}
    for block in blocks:
        block.setdefault("editor_feature", type_features.get(block.get("type"), "text"))
    editor_plan = [
        {
            "block_index": index,
            "feature": block["editor_feature"],
            **({"style": block["style"]} if block.get("type") in {"quote", "divider"} else {}),
        }
        for index, block in enumerate(blocks)
    ]
    quote_plan = [
        {"block_index": index, "text": block["text"], "style": block["style"]}
        for index, block in enumerate(blocks)
        if block.get("type") == "quote"
    ]
    return {
        "id": post_id,
        "title": title,
        "content": _text_from_blocks(blocks),
        "blocks": blocks,
        "editor_plan": editor_plan,
        "quote_plan": quote_plan,
        "images": images,
        "category": "",
        "keywords": keywords,
        "sources": sources,
        "editorial_plan": editorial_plan,
        "style": str(settings.get("style", "auto")),
        "length": "auto",
        "status": "draft",
        "image_count_auto": auto_count,
    }
