// TriageOps embedding service.
// Runs gte-small inside the Supabase Edge Runtime, so no third-party embedding API key is needed.
// Returns 384-dimension, mean-pooled, normalised vectors for pgvector cosine search.
// Auth is a shared secret header. The function stores only the SHA-256 of that secret,
// so the secret itself never lives in code or in this repository.
import "jsr:@supabase/functions-js/edge-runtime.d.ts";

// The edge runtime caps CPU time per request, so callers send a few short texts at a time.
const MAX_INPUTS = 4;
const MAX_CHARS = 2000;
const SECRET_SHA256 =
  Deno.env.get("EMBED_SECRET_SHA256") ??
  "32900d8e2325912aba69488c0fe0e353e3e311444b5d77be56e192940fbe3d22";

const session = new Supabase.ai.Session("gte-small");

async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, "0")).join("");
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

function safeEqual(a: string, b: string): boolean {
  const enc = new TextEncoder();
  const x = enc.encode(a);
  const y = enc.encode(b);
  if (x.length !== y.length) return false;
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}

Deno.serve(async (req: Request) => {
  if (req.method !== "POST") return json(405, { error: "Method not allowed" });

  const provided = req.headers.get("x-embed-secret") ?? "";
  if (!provided || !safeEqual(await sha256Hex(provided), SECRET_SHA256)) {
    return json(401, { error: "Unauthorised" });
  }

  let payload: { input?: unknown };
  try {
    payload = await req.json();
  } catch {
    return json(400, { error: "Body must be JSON" });
  }

  const raw = Array.isArray(payload.input) ? payload.input : [payload.input];
  const inputs = raw.filter((t): t is string => typeof t === "string" && t.trim().length > 0);
  if (inputs.length === 0 || inputs.length > MAX_INPUTS) {
    return json(400, { error: `input must be 1 to ${MAX_INPUTS} non-empty strings` });
  }

  const started = Date.now();
  const embeddings: number[][] = [];
  for (const text of inputs) {
    const vector = await session.run(text.slice(0, MAX_CHARS), { mean_pool: true, normalize: true });
    embeddings.push(Array.from(vector as Float32Array | number[]));
  }

  return json(200, { model: "gte-small", dimensions: 384, embeddings, ms: Date.now() - started });
});
