// Tiny Worker shim that fronts the FastAPI container.
//
// CF Containers always sit behind a Worker — the Worker accepts the request
// at the edge, then forwards it to a Container instance. `getContainer` picks
// (and if needed spins up) an instance from the binding's pool; `sleepAfter`
// scales it back to zero when idle, which keeps cost predictable.
//
// IMPORTANT: Worker secrets do NOT auto-propagate to the container's runtime
// environment — they have to be explicitly forwarded via `this.envVars` in
// the constructor. Without this, settings.database_url falls back to the dev
// default in config.py and the container tries to connect to localhost:5432.

import { Container, getContainer } from "@cloudflare/containers";

export class Api extends Container {
  defaultPort = 8088;       // matches EXPOSE in docker/Dockerfile
  sleepAfter = "10m";       // scale to zero after 10 min idle

  constructor(ctx, env) {
    super(ctx, env);
    // Forward Worker secrets into the container's process env.
    // Anything FastAPI / pydantic-settings reads from os.environ has to be
    // explicitly listed here.
    this.envVars = {
      DATABASE_URL: env.DATABASE_URL,
      CORS_ORIGINS: env.CORS_ORIGINS,
      GDELT_USER_AGENT: env.GDELT_USER_AGENT,
      SUMMARIZER_ENABLED: env.SUMMARIZER_ENABLED,
      INGEST_TOKEN: env.INGEST_TOKEN,
      // ANTHROPIC_API_KEY is only set if the summarizer is enabled —
      // forward it if present so a future flip works without a redeploy.
      ...(env.ANTHROPIC_API_KEY ? { ANTHROPIC_API_KEY: env.ANTHROPIC_API_KEY } : {}),
    };
  }
}

export default {
  async fetch(req, env) {
    return getContainer(env.API).fetch(req);
  },

  // Cron trigger entrypoint. Fires every 15 min per wrangler.jsonc triggers.
  // Posts to /admin/run-ingest with the shared token; the container returns
  // 202 immediately and runs run_all.py in a background thread. We don't
  // await the long pipeline here — the Worker scheduled handler has a tight
  // CPU budget. ctx.waitUntil ensures the request actually flies before we exit.
  async scheduled(event, env, ctx) {
    ctx.waitUntil(
      getContainer(env.API)
        .fetch("https://internal/admin/run-ingest", {
          method: "POST",
          headers: { "X-Admin-Token": env.INGEST_TOKEN },
        })
        .then((r) => console.log(`cron ingest trigger: ${r.status}`))
        .catch((e) => console.error("cron ingest trigger failed:", e))
    );
  },
};
