// Cloudflare Worker — fires the `daily-forecast` GitHub Actions workflow on a
// reliable schedule. GitHub's own `schedule:` cron on this repo runs 4-5h late
// or gets skipped entirely; Cloudflare's cron trigger is punctual, so we call
// the workflow_dispatch API from here instead.
//
// Secrets (set with `wrangler secret put ...`, never commit):
//   GH_TOKEN   fine-grained PAT on JonasBlancke/Dashboard, Actions: Read+Write
//
// Vars (in wrangler.toml [vars], safe to commit):
//   GH_OWNER, GH_REPO, GH_WORKFLOW, GH_REF
//
// The cron itself lives in wrangler.toml [triggers]. Manual test:
//   curl -H "Authorization: Bearer <SHARED_SECRET>" https://<worker-url>/run
// (only if you set SHARED_SECRET; otherwise /run is disabled.)

const UA = "dashboard-forecast-cron (+https://github.com/JonasBlancke/Dashboard)";

async function dispatch(env) {
  const url = `https://api.github.com/repos/${env.GH_OWNER}/${env.GH_REPO}/actions/workflows/${env.GH_WORKFLOW}/dispatches`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": UA,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: env.GH_REF || "main" }),
  });

  // 204 No Content = accepted. Anything else is a failure worth seeing in logs.
  const text = await res.text();
  if (res.status !== 204) {
    console.error(`dispatch failed: ${res.status} ${text}`);
    throw new Error(`GitHub returned ${res.status}: ${text}`);
  }
  console.log(`dispatched ${env.GH_WORKFLOW} @ ${env.GH_REF || "main"}`);
}

export default {
  // Cloudflare calls this on the schedule in wrangler.toml.
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(env));
  },

  // Optional manual trigger for testing. Disabled unless SHARED_SECRET is set.
  async fetch(req, env) {
    const { pathname } = new URL(req.url);
    if (pathname !== "/run") {
      return new Response("ok — this worker fires daily-forecast on a cron. POST /run with the shared secret to trigger now.\n");
    }
    if (!env.SHARED_SECRET) {
      return new Response("manual /run is disabled (no SHARED_SECRET set)\n", { status: 403 });
    }
    const auth = req.headers.get("Authorization") || "";
    if (auth !== `Bearer ${env.SHARED_SECRET}`) {
      return new Response("forbidden\n", { status: 403 });
    }
    try {
      await dispatch(env);
      return new Response("dispatched\n");
    } catch (e) {
      return new Response(`failed: ${e.message}\n`, { status: 502 });
    }
  },
};
