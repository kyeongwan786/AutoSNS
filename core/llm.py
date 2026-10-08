"""LLM 호출 (OpenAI 호환). 댓글, 나중에 포스팅에서 같이 씀."""
from __future__ import annotations
import os

from core import cloud

_client = None


def resolve_api_key(cfg_comment: dict) -> str:
    if cloud.supabase_configured():
        return "supabase-edge-function"
    return cfg_comment.get("api_key") or os.environ.get("OPENAI_API_KEY", "")


def create_openai_client(api_key: str = "", access_token: str | None = None, **kwargs):
    """Create an OpenAI client that routes licensed app users through Supabase."""
    from openai import OpenAI

    if cloud.supabase_configured():
        token = str(access_token or "").strip()
        if not token:
            raise cloud.CloudError("AI 기능을 사용하려면 앱에 다시 로그인해 주세요.")
        cfg = cloud.settings()
        base_url = f"{str(cfg['supabase_url']).rstrip('/')}/functions/v1/openai-proxy/v1"
        return OpenAI(api_key=token, base_url=base_url, **kwargs)
    resolved_key = str(api_key or os.environ.get("OPENAI_API_KEY", "")).strip()
    if not resolved_key:
        raise cloud.CloudError("OpenAI API 키를 설정하거나 클라우드 계정에 로그인해 주세요.")
    return OpenAI(api_key=resolved_key, **kwargs)


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
