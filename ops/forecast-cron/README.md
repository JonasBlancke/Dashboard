# forecast-cron — reliable trigger for `daily-forecast`

GitHub's `schedule:` cron on this repo runs 4–5 hours late or is skipped
entirely (shared runner queue, low-activity repo). This Cloudflare Worker fires
the same workflow via the `workflow_dispatch` API on Cloudflare's cron, which is
punctual. Nothing about the workflow itself changes — the Worker just presses
the button.

```
scheduled()  ──POST──▶  api.github.com/.../workflows/daily-forecast.yml/dispatches  { "ref": "main" }
```

## One-time setup

You need a Cloudflare account (free tier is enough) and `npx wrangler`.

### 1. Create the GitHub token

GitHub → Settings → Developer settings → **Fine-grained personal access tokens** → *Generate new token*:

- **Resource owner:** JonasBlancke
- **Repository access:** Only select repositories → `JonasBlancke/Dashboard`
- **Permissions → Repository permissions → Actions:** **Read and write**
- **Expiration:** 1 year (put a calendar reminder to rotate)

Copy the token (`github_pat_…`).

### 2. Deploy the Worker

```sh
cd ops/forecast-cron
npx wrangler login                 # opens the browser once
npx wrangler secret put GH_TOKEN   # paste the fine-grained PAT
# optional — enables `POST /run` for manual testing:
npx wrangler secret put SHARED_SECRET   # paste any long random string
npx wrangler deploy
```

### 3. Verify

```sh
# tail live logs, then trigger once:
npx wrangler tail &
curl -X POST -H "Authorization: Bearer <SHARED_SECRET>" https://dashboard-forecast-cron.<your-subdomain>.workers.dev/run
```

You should see `dispatched daily-forecast.yml @ main` in the tail, and a new
**workflow_dispatch** run appear under Actions within a few seconds.

The scheduled run then fires automatically every day at **04:43 UTC**.

## Changing the time

Edit `crons` in `wrangler.toml` and `npx wrangler deploy`. Keep it in sync with
the comment in `.github/workflows/daily-forecast.yml` (the GitHub cron can stay
as a harmless backup, or be removed).

## If a dispatch fails

`npx wrangler tail` shows the GitHub API response. Most likely causes:

| Response | Fix |
|---|---|
| `401` | token expired or wrong — regenerate, `wrangler secret put GH_TOKEN` again |
| `403` | token missing **Actions: write** on this repo |
| `404` | `GH_WORKFLOW` filename wrong, or workflow not on the default branch |
| `422` | `ref` (`main`) doesn't exist |
