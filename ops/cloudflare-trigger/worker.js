// Cloudflare Workers cron trigger for .github/workflows/paper.yml (docs/paper-trading.md,
// "Reliable hourly trigger"). GitHub drops many `schedule` runs when Actions is busy, so this worker
// calls workflow_dispatch every hour at minute 37 (UTC). The workflow's own `schedule` is a sparse backup.
//
// Optional second cron, minute 47 (`47 * * * *`): a catch-up. It dispatches only when this hour has no
// paper.yml run that succeeded or is still queued/running (the :37 cron did not fire, or its dispatch or
// run failed), so it costs no Actions minutes in a normal hour.
//
// Secret: GITHUB_TOKEN, a fine-grained PAT limited to this repository with Actions: Read and write.
// No dependencies; paste this file into the Workers dashboard editor.

const REPO_API = "https://api.github.com/repos/taejunoh/elliott-wave-btc-bot/actions/workflows/paper.yml";
const CATCH_UP_CRON = "47 * * * *";

function headers(env) {
  return {
    Authorization: `Bearer ${env.GITHUB_TOKEN}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "ewbot-paper-trigger",
    "Content-Type": "application/json",
  };
}

// true when a paper.yml run created since the start of this UTC hour succeeded or has not finished yet
async function hourCovered(env, now) {
  const hour = new Date(now);
  hour.setUTCMinutes(0, 0, 0);
  const url = `${REPO_API}/runs?per_page=20&created=${encodeURIComponent(">=" + hour.toISOString())}`;
  const response = await fetch(url, { headers: headers(env) });
  if (!response.ok) {
    console.error(`listing runs failed: HTTP ${response.status}`);
    return false; // unknown: dispatch (one extra run at worst)
  }
  const { workflow_runs: runs = [] } = await response.json();
  return runs.some((r) => r.status !== "completed" || r.conclusion === "success");
}

export default {
  async scheduled(event, env, ctx) {
    if (event.cron === CATCH_UP_CRON && (await hourCovered(env, event.scheduledTime))) {
      return; // the :37 run is there: nothing to catch up
    }
    const response = await fetch(`${REPO_API}/dispatches`, {
      method: "POST",
      headers: headers(env),
      body: JSON.stringify({ ref: "master" }),
    });
    if (!response.ok) { // 204 (classic) or 200 (newer API versions)
      const text = await response.text();
      // never log the token or the request headers
      console.error(`workflow_dispatch failed: HTTP ${response.status} ${text}`);
      throw new Error(`workflow_dispatch failed: HTTP ${response.status}`);
    }
    if (event.cron === CATCH_UP_CRON) console.log("catch-up dispatch: no paper.yml run this hour");
  },

  // no public trigger: the worker only runs from its cron
  async fetch() {
    return new Response("Not found", { status: 404 });
  },
};
