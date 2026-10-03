// Cloudflare Workers cron trigger for .github/workflows/paper.yml (docs/paper-trading.md,
// "Reliable hourly trigger"). GitHub drops many `schedule` runs when Actions is busy, so this worker
// calls workflow_dispatch every hour at minute 37 (UTC). The workflow's own `schedule` is a sparse backup.
//
// Secret: GITHUB_TOKEN, a fine-grained PAT limited to this repository with Actions: Read and write.
// No dependencies; paste this file into the Workers dashboard editor.

const DISPATCH_URL =
  "https://api.github.com/repos/taejunoh/elliott-wave-btc-bot/actions/workflows/paper.yml/dispatches";

export default {
  async scheduled(event, env, ctx) {
    const response = await fetch(DISPATCH_URL, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ewbot-paper-trigger",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: "master" }),
    });
    if (!response.ok) { // 204 (classic) or 200 (newer API versions)
      const text = await response.text();
      // never log the token or the request headers
      console.error(`workflow_dispatch failed: HTTP ${response.status} ${text}`);
      throw new Error(`workflow_dispatch failed: HTTP ${response.status}`);
    }
  },

  // no public trigger: the worker only runs from its cron
  async fetch() {
    return new Response("Not found", { status: 404 });
  },
};
