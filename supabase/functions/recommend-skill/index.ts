// Deployed skill recommender: embeds the query with the edge runtime's built-in
// gte-small (same weights the local pipeline uses), runs the hybrid pgvector+FTS
// RPC, and applies the same recommend/clarify/none rule as the local server's
// heuristic path. No LLM here — replies are templated so the public site works
// with zero paid (or local) dependencies.
import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "jsr:@supabase/supabase-js@2";

const session = new Supabase.ai.Session("gte-small");
const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_ANON_KEY")!,
);

// TODO: tighten to the Vercel domain once it exists.
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

const RECOMMEND_GAP = 1.6;

const NONE_MESSAGE =
  "I couldn't find anything matching that. Try describing the task with " +
  "different words — e.g. the tool, file type, or service involved.";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...CORS },
  });
}

interface Skill {
  name: string;
  description: string | null;
  source: string;
  url: string;
  tags: string[] | null;
  stars: number;
  risk_score: number;
  rank: number;
}

function blurb(s: Skill): string {
  const parts = [`Best match: ${s.name}.`];
  if (s.description) parts.push(s.description.slice(0, 200));
  if (s.stars) parts.push(`(${s.stars} GitHub stars)`);
  if ((s.risk_score ?? 0) > 0) {
    parts.push(
      `Note: the malware scan gave this a low-level risk score of ${s.risk_score} — review it before installing.`,
    );
  }
  return parts.join(" ");
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  if (req.method !== "POST") return json({ error: "POST only" }, 405);

  let messages: { role?: string; content?: string }[] = [];
  try {
    const body = await req.json();
    messages = Array.isArray(body.messages) ? body.messages : [];
  } catch {
    return json({ error: "invalid JSON body" }, 400);
  }

  const userTexts = messages
    .filter((m) => m.role === "user" && typeof m.content === "string" && m.content.trim())
    .map((m) => m.content as string);
  if (!userTexts.length) {
    return json({ type: "none", message: "Tell me what you're trying to do and I'll find a skill for it." });
  }
  const query = userTexts.join(" ").slice(-500);

  let embedding: number[];
  try {
    const vec = await session.run(query, { mean_pool: true, normalize: true });
    embedding = Array.from(vec as number[]);
  } catch (e) {
    return json({ type: "none", message: `Embedding failed — try again in a moment. (${String(e).slice(0, 100)})` }, 500);
  }

  const { data, error } = await supabase.rpc("hybrid_search_skills", {
    query_text: query,
    query_embedding: `[${embedding.join(",")}]`,
    match_count: 10,
  });
  if (error) {
    return json({ type: "none", message: "Search failed — try again in a moment." }, 500);
  }

  const safe: Skill[] = (data ?? []).filter((s: Skill) => (s.risk_score ?? 0) < 3);
  if (!safe.length) return json({ type: "none", message: NONE_MESSAGE });

  const top = safe[0];
  const runnerUp = safe[1];
  if (!runnerUp || top.rank >= runnerUp.rank * RECOMMEND_GAP) {
    return json({ type: "recommend", skill: top, message: blurb(top) });
  }
  return json({
    type: "clarify",
    message:
      "A few skills fit that about equally well — which of these is closest to what you're doing? Pick one, or describe your task in a bit more detail.",
    options: safe.slice(0, 3),
  });
});
