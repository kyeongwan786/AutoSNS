import { createClient } from "npm:@supabase/supabase-js@2";

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

Deno.serve(async (request: Request) => {
  if (request.method === "OPTIONS") return new Response("ok", { headers: cors });
  const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
    status, headers: { ...cors, "Content-Type": "application/json" },
  });
  if (request.method !== "POST") return json({ error: "Method not allowed" }, 405);

  const authorization = request.headers.get("Authorization") ?? "";
  const accessToken = authorization.replace(/^Bearer\s+/i, "");
  if (!accessToken) return json({ error: "로그인이 필요합니다." }, 401);

  const url = Deno.env.get("SUPABASE_URL")!;
  const anonKey = Deno.env.get("SUPABASE_ANON_KEY")!;
  const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
  const userClient = createClient(url, anonKey, {
    global: { headers: { Authorization: `Bearer ${accessToken}` } },
  });
  const { data: { user }, error: authError } = await userClient.auth.getUser();
  if (authError || !user) return json({ error: "세션이 만료되었습니다. 앱에 다시 로그인하세요." }, 401);

  const admin = createClient(url, serviceKey, { auth: { persistSession: false } });
  const { data: license } = await admin.from("customer_licenses")
    .select("status,expires_at").eq("user_id", user.id).maybeSingle();
  if (!license || license.status !== "active" ||
      (license.expires_at && Date.parse(license.expires_at) <= Date.now())) {
    return json({ error: "사용 권한이 없거나 만료되었습니다." }, 403);
  }
  let input: { prompt?: string; title?: string; body?: string; model?: string };
  try { input = await request.json(); } catch { return json({ error: "요청 형식이 올바르지 않습니다." }, 400); }
  const title = String(input.title ?? "").slice(0, 500);
  const body = String(input.body ?? "").slice(0, 2000);
  const prompt = String(input.prompt ?? "").slice(0, 6000);
  const model = String(input.model ?? "gpt-4o-mini");
  if (!prompt || !title || !body || !["gpt-4o-mini", "gpt-4.1-mini"].includes(model)) {
    return json({ error: "댓글 생성 입력값을 확인하세요." }, 400);
  }

  const { data: quotaAvailable, error: quotaError } = await admin.rpc(
    "claim_comment_quota", { p_user_id: user.id },
  );
  if (quotaError || !quotaAvailable) return json({ error: "오늘의 댓글 사용 한도에 도달했습니다." }, 429);

  const openaiResponse = await fetch("https://api.openai.com/v1/chat/completions", {
    method: "POST",
    headers: { Authorization: `Bearer ${Deno.env.get("OPENAI_API_KEY")}`, "Content-Type": "application/json" },
    body: JSON.stringify({ model, messages: [{ role: "user", content: prompt
      .replace(/\{(title|body)\}/g, (_match, key) => key === "title" ? title : body) }], temperature: 0.9, max_tokens: 120 }),
  });
  if (!openaiResponse.ok) return json({ error: "댓글 생성 서비스에 연결하지 못했습니다." }, 502);
  const completion = await openaiResponse.json();
  const comment = String(completion.choices?.[0]?.message?.content ?? "").trim();
  return json({ comment });
});
