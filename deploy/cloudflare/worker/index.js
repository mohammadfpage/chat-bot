import { Container } from "@cloudflare/containers";

// Must match config.py's webhook_path default — used only when the Worker
// binding WEBHOOK_PATH is missing entirely.
const DEFAULT_HOOK_PATH = "/telegram/webhook";

/**
 * Mirror of config.py's webhook_path normalisation: trim, fall back to the
 * default when empty, force ONE leading slash, and otherwise keep the string
 * byte-identical (Settings does NOT strip a trailing slash either — any
 * divergence here would make the Worker 404 every Telegram update while the
 * container's aiohttp waits on a slightly different path).
 */
function hookPath(raw) {
  const trimmed = (raw ?? "").trim();
  if (!trimmed) return DEFAULT_HOOK_PATH;
  return trimmed.startsWith("/") ? trimmed : `/${trimmed}`;
}

export class BotContainer extends Container {
  // The aiohttp server inside the image binds WEBAPP_HOST:WEBAPP_PORT =
  // 0.0.0.0:8080 (wrangler.jsonc vars → bot.py run_webhook). The class
  // pings this port before proxying, so the first request after a sleep
  // waits for a real listener instead of a half-booted process.
  defaultPort = 8080;

  // Idle for 10 minutes → the platform sends SIGTERM (never SIGKILL) and
  // the process exits. Nothing is lost: queue/pairs live in PostgreSQL
  // (chat_pairs + rebuild_chat_state), FSM state in Redis, and the next
  // webhook or /health request boots a fresh process that restores both.
  sleepAfter = "10m";

  constructor(ctx, env, options) {
    super(ctx, env, options);
    // The platform injects only the CLOUDFLARE_* runtime variables into the
    // container — every wrangler var and every `wrangler secret put` value
    // has to be forwarded explicitly. envVars is read fresh on each start,
    // which is what makes a re-put secret (or an edited var) take effect on
    // the next wake instead of requiring a redeploy.
    const forwarded = {};
    for (const [key, value] of Object.entries(env)) {
      // Skip bindings (Durable Object namespaces are objects, not strings).
      if (typeof value === "string") forwarded[key] = value;
    }
    this.envVars = forwarded;
  }
}

export default {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);

    const isTelegram =
      request.method === "POST" && pathname === hookPath(env.WEBHOOK_PATH);
    const isHealth = pathname === "/health";

    // Path gate: everything else gets a plain 404 WITHOUT touching the
    // Durable Object, so scanners, wrong URLs and stray GETs can never
    // cold-boot (or keep alive) the bot container. Wake = paid.
    if (!isTelegram && !isHealth) {
      return new Response("Not found", { status: 404 });
    }

    // ONE named instance = ONE container (max_instances is 1 in
    // wrangler.jsonc: pair_map/search_queue live in process RAM, and a
    // second instance would split users across disjoint worlds).
    return env.BOT_CONTAINER.getByName("bot").fetch(request);
  },
};
