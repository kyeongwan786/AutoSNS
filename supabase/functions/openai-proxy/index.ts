import { createClient } from "npm:@supabase/supabase-js@2";

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
const MAX_BODY_BYTES = 1_000_000;
const ALLOWED_MODELS: Record<string, Set<string>> = {
  responses: new Set(["gpt-4.1"]),
  "chat/completions": new Set(["gpt-4.1"]),
  "images/generations": new Set(["gpt-image-2"]),
};

Deno.serve(async (request: Request) => {
  const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
    status,
    headers: { ...cors, "Content-Type": "application/json" },
  });
  if (request.method === "OPTIONS") return new Response("ok", { headers: cors });
  if (request.method !== "POST") return json({ error: { message: "Method not allowed" } }, 405);

  const accessToken = (request.headers.get("Authorization") ?? "").replace(/^Bearer\s+/i, "");
  if (!accessToken) return json({ error: { message: "로그인이 필요합니다." } }, 401);

  const url = Deno.env.get("SUPABASE_URL");
  const anonKey = Deno.env.get("SUPABASE_ANON_KEY");
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  const openAiKey = Deno.env.get("OPENAI_API_KEY");
  if (!url || !anonKey || !serviceKey || !openAiKey) {
    return json({ error: { message: "AI 서버 설정을 확인해 주세요." } }, 503);
  }

  const userClient = createClient(url, anonKey, {
    global: { headers: { Authorization: `Bearer ${accessToken}` } },
  });
  const { data: { user }, error: authError } = await userClient.auth.getUser();
  if (authError || !user) return json({ error: { message: "세션이 만료되었습니다. 다시 로그인해 주세요." } }, 401);

  const admin = createClient(url, serviceKey, { auth: { persistSession: false } });
  const { data: license, error: licenseError } = await admin
    .from("customer_licenses")
    .select("status,expires_at")
    .eq("user_id", user.id)
    .maybeSingle();
  if (licenseError || !license || license.status !== "active" ||
      (license.expires_at && Date.parse(license.expires_at) <= Date.now())) {
    return json({ error: { message: "사용 권한이 없거나 만료되었습니다." } }, 403);
  }

  const pathname = new URL(request.url).pathname;
  const marker = "/openai-proxy/";
  const markerIndex = pathname.indexOf(marker);
  const endpoint = markerIndex >= 0 ? pathname.slice(markerIndex + marker.length).replace(/^v1\//, "") : "";
  if (!(endpoint in ALLOWED_MODELS)) {
    return json({ error: { message: "허용되지 않은 AI 요청입니다." } }, 404);
  }

  const contentLength = Number(request.headers.get("content-length") ?? 0);
  if (contentLength > MAX_BODY_BYTES) return json({ error: { message: "요청 크기가 너무 큽니다." } }, 413);
  let raw: string;
  let body: Record<string, unknown>;
  try {
    raw = await request.text();
    if (new TextEncoder().encode(raw).byteLength > MAX_BODY_BYTES) {
      return json({ error: { message: "요청 크기가 너무 큽니다." } }, 413);
    }
    body = JSON.parse(raw);
  } catch {
    return json({ error: { message: "요청 형식이 올바르지 않습니다." } }, 400);
  }

  const model = String(body.model ?? "");
  if (!ALLOWED_MODELS[endpoint].has(model)) {
    return json({ error: { message: "허용되지 않은 모델입니다." } }, 400);
  }
  if (endpoint === "images/generations" && body.n !== undefined && body.n !== 1) {
    return json({ error: { message: "이미지는 한 번에 한 장씩 생성할 수 있습니다." } }, 400);
  }
  for (const key of ["max_tokens", "max_completion_tokens", "max_output_tokens"]) {
    const value = body[key];
    if (typeof value === "number" && value > 7000) {
      return json({ error: { message: "요청 토큰 한도를 초과했습니다." } }, 400);
    }
  }

  const upstream = await fetch(`https://api.openai.com/v1/${endpoint}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${openAiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  const responseBody = await upstream.text();
  return new Response(responseBody, {
    status: upstream.status,
    headers: {
      ...cors,
      "Content-Type": upstream.headers.get("content-type") ?? "application/json",
      ...(upstream.headers.get("retry-after") ? { "Retry-After": upstream.headers.get("retry-after")! } : {}),
    },
  });
});
