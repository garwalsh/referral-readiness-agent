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
