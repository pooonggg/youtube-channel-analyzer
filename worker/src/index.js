// Viewer-status store for the YouTube library site (Cloudflare Worker + KV, free tier).
//   GET  /status  -> {"v":1,"items":{videoId:{"s":status|null,"t":epoch_ms}}}   (public read)
//   PUT  /status  -> same shape; merged per clip, newest "t" wins. Needs  Authorization: Bearer <WRITE_KEY>
// WRITE_KEY is a Worker secret (never in the repo). Only the site's origin may call it from a browser.
const ORIGINS = new Set(["https://pooonggg.github.io"]);
const ID_RE = /^[A-Za-z0-9_-]{11}$/;
const STATUSES = new Set(["watched", "interested", "not_interested"]);
const KV_KEY = "status";
const MAX_BODY = 200_000;

function corsHeaders(req) {
  const h = {
    Vary: "Origin",
    "Access-Control-Allow-Methods": "GET, PUT, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Max-Age": "86400",
  };
  const origin = req.headers.get("Origin");
  if (origin && ORIGINS.has(origin)) h["Access-Control-Allow-Origin"] = origin;
  return h;
}

function reply(req, body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...corsHeaders(req) },
  });
}

function sameSecret(a, b) {
  const x = new TextEncoder().encode(a), y = new TextEncoder().encode(b);
  if (x.length !== y.length) return false;
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}

async function load(env) {
  const raw = await env.STATUS.get(KV_KEY);
  try { return (raw && JSON.parse(raw).items) || {}; } catch { return {}; }
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: corsHeaders(req) });
    if (url.pathname !== "/status") return reply(req, { error: "not found" }, 404);

    if (req.method === "GET") return reply(req, { v: 1, items: await load(env) });

    if (req.method === "PUT") {
      const auth = (req.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
      if (!env.WRITE_KEY || !auth || !sameSecret(auth, env.WRITE_KEY)) return reply(req, { error: "unauthorized" }, 401);
      const text = await req.text();
      if (text.length > MAX_BODY) return reply(req, { error: "too large" }, 413);
      let incoming;
      try { incoming = JSON.parse(text).items; } catch { return reply(req, { error: "bad json" }, 400); }
      if (!incoming || typeof incoming !== "object") return reply(req, { error: "bad items" }, 400);

      const items = await load(env);
      for (const [id, v] of Object.entries(incoming)) {
        const s = v && v.s, t = v && v.t;
        if (!ID_RE.test(id) || !(s === null || STATUSES.has(s)) || typeof t !== "number" || !isFinite(t)) {
          return reply(req, { error: "bad item " + id }, 400);
        }
        const cur = items[id];
        if (!cur || t > cur.t) items[id] = { s, t };   // newest press wins, per clip
      }
      if (Object.keys(items).length > 5000) return reply(req, { error: "too many items" }, 413);
      await env.STATUS.put(KV_KEY, JSON.stringify({ v: 1, items }));
      return reply(req, { v: 1, items });
    }
    return reply(req, { error: "method not allowed" }, 405);
  },
};
