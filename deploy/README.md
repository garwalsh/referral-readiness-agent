# Run the dashboard and worker

## Run the dashboard locally

Create the Python 3.12 virtual environment from the [README](../README.md). Configure a synthetic fixture map using the [fixture guide](../docs/fixtures.md#map-loaded-fixtures). The web process reads the map to populate the patient selector. The worker also needs the connection settings for the selected transport.

```sh
python -m pip install -r dashboard/requirements.txt
export DJANGO_DEBUG=1
export DJANGO_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver 127.0.0.1:8000
```

Run `python manage.py run_review_worker` in a second shell with the same Django settings and SQLite path. The default database is `db.sqlite3` in the project root. Keep the same private Django secret across restarts to preserve signed sessions. Use debug mode only for local development.

## StackOne Gateway

Set `STACKONE_ACCOUNT_ID` to your linked LangCare account ID. The [adapter](../scripts/stackone.py) currently uses the demo connector's two action IDs. A different connector registration may require changing those constants to match the IDs in that Gateway.

```sh
python scripts/stackone.py login
python scripts/stackone.py probe "$PATIENT_ID"
RRR_TRANSPORT=stackone python scripts/run_agent.py "$PATIENT_ID" --review-date 2026-11-25
```

In the consent screen, select the linked LangCare account and only FHIR Search and Read. Leave **Load tools when needed** enabled. The callback listens on loopback during sign-in. The CLI defaults to storing the member grant at `~/.config/rrr/stackone-oauth.json`. Use `STACKONE_OAUTH_FILE` to specify a private alternate path. The probe performs one patient-scoped search and prints the count and Gateway action metadata when available.

The worker refreshes the grant without a browser. A file lock serializes refreshes and the token store is replaced atomically. Use separate grants for the worker and local testing because refresh can rotate the token. Revoked grants fail visibly and require reauthorization. The adapter never switches to direct LangCare automatically.

## Docker Compose

The [Compose file](compose.yaml) runs Gunicorn with WhiteNoise and a separate worker from the same image. It expects the external Docker network `rrr_default` with a TLS reverse proxy on that network forwarding to `rrr-dashboard:8000`. Keep the application private to the proxy network. Keep debug mode off and only trust forwarded headers when the proxy controls them.

Keep the data and runtime files under `RRR_DEPLOY_DIR` outside the source checkout. Its default is `/opt/rrr-dashboard`.

- `dashboard.env` is shared by the web and worker services. Set `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`, `DJANGO_TRUST_PROXY=1`, `DJANGO_USE_WHITENOISE=1`, `DJANGO_DB_PATH=/var/lib/rrr/db.sqlite3`, `RRR_RUN_DIR=/var/lib/rrr/traces`, and `RRR_FIXTURE_MAP=/etc/rrr/fixture-ids.json`. Use your own dashboard hostname and its HTTPS origin for the host and CSRF settings.
- `fixture-ids.json` is the synthetic fixture map. Both services mount it read-only. The `data/` mount holds SQLite and traces and must be writable by UID 10001.
- `runner.env` is worker-only. Set `ANTHROPIC_API_KEY`, `RRR_TRANSPORT=stackone`, and `STACKONE_ACCOUNT_ID` for Gateway. For direct LangCare set `RRR_TRANSPORT=langcare`, `LANGCARE_MCP_URL`, and `LANGCARE_MCP_TOKEN`.
- For StackOne, transfer the worker's authorized grant through a secure channel to `stackone-auth/stackone-oauth.json`. The directory and file must be owned by UID 10001 with modes 0700 and 0600 respectively. The web service has no access to this mount.

Use the same Compose project name throughout. Build the image, initialize the shared database, and start the services from the repository root.

```sh
docker compose -p rrr-dashboard -f deploy/compose.yaml build dashboard
docker compose -p rrr-dashboard -f deploy/compose.yaml run --rm dashboard python manage.py migrate
docker compose -p rrr-dashboard -f deploy/compose.yaml run --rm dashboard python manage.py createsuperuser
docker compose -p rrr-dashboard -f deploy/compose.yaml up -d
```

For an explicit rollback to direct LangCare, set `RRR_TRANSPORT=langcare` and supply its credentials in `runner.env`. When the queue is idle, recreate only the worker.

```sh
docker compose -p rrr-dashboard -f deploy/compose.yaml up -d --no-deps --no-build --force-recreate worker
```

## Inspect saved evaluations

Django reads completed run directories from `evals/output/` by default. Use `RRR_EVAL_DIR` to override that path locally. For Compose, copy each complete run directory to `evaluations/` under the deployment directory, preserving all relative paths. Verify the copy before exposing it to the dashboard, and grant UID 10001 read and traversal access. The web service mounts the archive read-only.

Administrators can inspect case outcomes, citations, traces, exact historical prompts and prompt diffs in **Evaluations**. Keep the archive and worker traces private.

For a web-only UI release, rebuild and recreate only the dashboard. The running worker continues on its current image.

```sh
docker compose -p rrr-dashboard -f deploy/compose.yaml up -d --build --no-deps dashboard
```
