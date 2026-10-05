"""LLM 호출 (OpenAI 호환). 댓글, 나중에 포스팅에서 같이 씀."""
from __future__ import annotations
import os

from core import cloud

_client = None


def resolve_api_key(cfg_comment: dict) -> str:
    if cloud.supabase_configured():
        return "supabase-edge-function"
    return cfg_comment.get("api_key") or os.environ.get("OPENAI_API_KEY", "")


async def make_comment(cfg_comment: dict, title: str, body: str) -> str | None:
    """댓글 한 문장 생성. 광고/내용 부족이면 None"""
    global _client
    if cloud.supabase_configured():
        import asyncio
        return await asyncio.to_thread(
            cloud.generate_comment, cfg_comment["prompt"], title, body,
            cfg_comment["model"], cfg_comment["max_body_chars"],
        )
    if _client is None:
        from openai import AsyncOpenAI
        _client = AsyncOpenAI(api_key=resolve_api_key(cfg_comment))

    prompt = cfg_comment["prompt"].format(title=title, body=body[: cfg_comment["max_body_chars"]])
    resp = await _client.chat.completions.create(
        model=cfg_comment["model"],
        messages=[{"role": "user", "content": prompt}],
        temperature=0.9,
        max_tokens=120,
    )
    text = (resp.choices[0].message.content or "").strip().strip('"').strip("'")
    if not text or "SKIP" in text.upper():
        return None
    return text.splitlines()[0][:150]
