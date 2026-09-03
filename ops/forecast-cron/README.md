# forecast-cron — reliable trigger for `daily-forecast`

GitHub's `schedule:` cron on this repo runs 4–5 hours late or is skipped
entirely (shared runner queue, low-activity repo). This Cloudflare Worker fires
the same workflow via the `workflow_dispatch` API on Cloudflare's cron, which is
punctual. Nothing about the workflow itself changes — the Worker just presses
the button.

```
scheduled()  ──POST──▶  api.github.com/.../workflows/daily-forecast.yml/dispatches  { "ref": "main" }
```

---

## Setup A — Cloudflare dashboard, no install (recommended)

This machine has no Node/npm, so `wrangler` isn't an option. Do it all in the
browser instead. ~5 minutes, one time.

### 1. Create the GitHub token

GitHub → your avatar → **Settings** → **Developer settings** (bottom of the left
menu) → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**:

- **Token name:** `dashboard-forecast-cron`
- **Expiration:** 1 year (set a reminder to rotate it)
- **Resource owner:** JonasBlancke
- **Repository access:** *Only select repositories* → `JonasBlancke/Dashboard`
- **Permissions → Repository permissions → Actions:** set to **Read and write**
- **Generate token**, then copy the `github_pat_…` string (shown once).

### 2. Create the Worker

1. [dash.cloudflare.com](https://dash.cloudflare.com) → **Workers & Pages** →
   **Create** → **Create Worker**.
2. Name it `dashboard-forecast-cron` → **Deploy** (deploys the default
   hello-world; we replace it next).
3. **Edit code** → select all, delete, and paste the full contents of
   [`worker.js`](worker.js) → **Deploy**.

### 3. Add the variables

Worker → **Settings** → **Variables and Secrets** → **Add**:

| Name | Type | Value |
|---|---|---|
| `GH_OWNER` | Text | `JonasBlancke` |
| `GH_REPO` | Text | `Dashboard` |
| `GH_WORKFLOW` | Text | `daily-forecast.yml` |
| `GH_REF` | Text | `main` |
| `GH_TOKEN` | **Secret** | the `github_pat_…` from step 1 |
| `SHARED_SECRET` | **Secret** | any long random string (enables manual `POST /run`) |

**Deploy** after adding them.

### 4. Add the cron

Worker → **Settings** → **Triggers** → **Cron Triggers** → **Add Cron Trigger** →
enter the schedule → **Add**.

- Normal schedule: `43 4 * * *`  (04:43 UTC ≈ 06:43 Brussels)
- For a same-day test you can temporarily use a time ~10 min from now, then
  change it back.

### 5. Verify

Two ways:

- **Dashboard:** in the Worker's editor, open the **Triggers**/schedule panel and
  use **"Trigger scheduled event"**, or
- **HTTP:** `POST` to the Worker URL with your shared secret:
  ```
  curl -X POST -H "Authorization: Bearer <SHARED_SECRET>" \
    https://dashboard-forecast-cron.<your-subdomain>.workers.dev/run
  ```

Either way: within a few seconds a new **workflow_dispatch** run of
`daily-forecast` appears under the repo's **Actions** tab, and ~10–15 min later a
`chore(data): … forecast` commit lands on `main`. The site redeploys from that
push.

---

## Setup B — wrangler CLI (needs Node)

If you install Node (`winget install OpenJS.NodeJS.LTS`, then a fresh terminal):

```sh
cd ops/forecast-cron
npx wrangler login
npx wrangler secret put GH_TOKEN
npx wrangler secret put SHARED_SECRET   # optional
npx wrangler deploy
```

`wrangler.toml` already carries the vars and cron. Tail logs with
`npx wrangler tail`.

---

## Changing the time

- **Dashboard:** Worker → Settings → Triggers → edit the cron.
- **wrangler:** edit `crons` in `wrangler.toml`, `npx wrangler deploy`.

Keep it roughly in sync with the comment in
`.github/workflows/daily-forecast.yml`. The GitHub `schedule:` cron can stay as a
harmless backup.

## If a dispatch fails

The Worker logs the GitHub API response (dashboard → Worker → **Logs**, or
`npx wrangler tail`). Most likely causes:

| Response | Fix |
|---|---|
| `401` | token expired or wrong — regenerate, update the `GH_TOKEN` secret |
| `403` | token missing **Actions: write** on this repo |
| `404` | `GH_WORKFLOW` filename wrong, or workflow not on the default branch |
| `422` | `GH_REF` (`main`) doesn't exist |
