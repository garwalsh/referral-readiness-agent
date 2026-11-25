# RRR deployment addresses

| Service | Public address |
| --- | --- |
| RRR Operations Dashboard | [referralsagent.garwalsh.com](https://referralsagent.garwalsh.com/) |
| OpenEMR browser access | [emr.garwalsh.com](https://emr.garwalsh.com/) |
| Preserved OpenEMR integration origin | [rrr.garwalsh.com](https://rrr.garwalsh.com/) |
| LangCare MCP | `https://mcp.rrr.garwalsh.com/mcp` |

Both new hostnames have A records pointing to `64.225.121.106`. The existing Caddy service terminates HTTPS. It proxies the dashboard to `rrr-dashboard:8000` and both OpenEMR hostnames to `openemr:80` over the existing Docker network. The previous dashboard hostname redirects to the canonical URL and preserves the request URI.

The dashboard service reads these public-origin settings from its private runtime environment:

```dotenv
DJANGO_ALLOWED_HOSTS=referralsagent.garwalsh.com,rrr-dashboard.64.225.121.106.sslip.io
DJANGO_CSRF_TRUSTED_ORIGINS=https://referralsagent.garwalsh.com
DJANGO_TRUST_PROXY=1
```

The previous hostname is retained in Django's host list for rollback. Caddy serves its redirect.

Keep the existing dashboard secret and persistent data mounts when recreating the web service. The worker uses the same SQLite database and trace directory. Browser sessions are scoped to the hostname, so sign in on the canonical dashboard after moving from the previous URL.

Keep OpenEMR's configured FHIR base URL, OAuth issuer, registered clients, and callback URLs on their existing settings. The preferred browser hostname is an additional Caddy route. LangCare continues to use the existing integration origin.

## Evaluation artifacts

Transfer each completed synthetic evaluation directory from `evals/output/` to the host's `${RRR_DEPLOY_DIR:-/opt/rrr-dashboard}/evaluations/`. Preserve the directory name and all relative files, including the JSON, summary, traces, and reports. Verify the copy before moving it into the evaluation root. Give the dashboard container user (UID 10001) read access to the files and traversal access to the directories. The web service mounts this directory read-only. It stays separate from SQLite, worker traces, and the Docker build context.

Rebuild and recreate only the web service for an evaluation UI release. The existing worker can continue processing on its current image.

```sh
docker compose -p rrr-dashboard -f deploy/compose.yaml up -d --build --no-deps dashboard
```

## Worker integration transport

The deployed worker uses `RRR_TRANSPORT=stackone` and the existing LangCare linked account ID in `runner.env`. Its OAuth grant is stored in the worker-only bind mount at `stackone-auth/stackone-oauth.json` under the deployment directory. The directory and file must be owned by UID 10001 with modes 0700 and 0600 respectively. The web service has no access to this mount. Keep the original LangCare credentials in `runner.env` for explicit rollback.

Authorize the worker grant interactively with `python3 scripts/stackone.py login` on a trusted machine. Transfer its owner-only JSON directly into the worker mount over SSH, outside the source tree. Provision a different grant for subsequent local testing. Refresh can rotate the token, so two independent token files must not keep using the same grant. The worker locks the credential directory during refresh and atomically saves the token response. A revoked or expired grant fails visibly without retrying via LangCare.

To return to direct LangCare, set `RRR_TRANSPORT=langcare` in the private `runner.env`. When the queue is idle, recreate only the worker using the existing Compose project. The reviewed case history, traces and evaluation archives stay in their existing mounts.

```sh
docker compose -p rrr-dashboard -f deploy/compose.yaml up -d --no-deps --no-build --force-recreate worker
```
