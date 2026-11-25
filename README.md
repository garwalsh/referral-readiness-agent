# Referral Readiness Review (RRR) Agent

The Referral Readiness Review Agent is a technical portfolio project that uses synthetic electronic health records to assess documentation for a proposed cardiology referral for palpitations. A `READY` result indicates that the record satisfies the demo checklist. Clinical decisions remain with the care team.

## Current state

Patient search and clinical record retrieval have been tested end to end through **Claude → StackOne Gateway → LangCare MCP → OpenEMR FHIR API**. The clinical integration uses read-only FHIR access.

The standalone Python agent uses **Claude → LangCare MCP → OpenEMR FHIR API**. It returns a validated JSON report with one finding per checklist item. The offline fixture generator and a one-case loader for an isolated OpenEMR restore are available. The standalone evaluation harness scores the nine generated cases.

## Run a synthetic review

Install the reporting dependency with `python3 -m pip install -r requirements.txt`. Set `ANTHROPIC_API_KEY`, `LANGCARE_MCP_URL`, and `LANGCARE_MCP_TOKEN` in the environment or the Git-ignored `.env`. The runner defaults to Haiku 5.5. Set `--model` to compare models on the same patient and review date.

```sh
python3 scripts/run_agent.py a34c2416-8263-4819-9acf-690e2e6fdd01 \
  --review-date 2026-11-25
```

The prompt asks Claude to treat clinical narratives as untrusted evidence, interpret the checklist, and propose supporting IDs and note excerpts. Python enforces patient scope, the read-only tool allowlist, and the ten-attempt cap. It validates all five findings, verifies each citation and excerpt against that run's retrieved chart, checks measured blood pressure and heart rate, and applies the inclusive 90-day ECG rule. Claude interprets which components the excerpts support. Python renders the explanations from checklist fields and calculates readiness.

A completed, unfiltered search can establish a documentation gap even if it returns zero results. Incomplete retrieval leaves unsupported findings unknown. The JSONL trace under `logs/` records run metadata, tool parameters and durations, retrieved IDs, validation failures, and final statuses. Ordinary tool events omit clinical narratives. A rejected citation records its submitted excerpt, FHIR resource, requirement, and reason. If a text comparison failed, the event also retains the original compared attachments from that cited, patient-scoped resource.

### Try the StackOne Gateway path locally

The standalone agent can use a separate StackOne member OAuth grant. Use your existing LangCare linked account ID as `STACKONE_ACCOUNT_ID` in the environment or ignored `.env`. The project API key is unnecessary for this route.

```sh
python3 scripts/stackone.py login
python3 scripts/stackone.py probe a34c2416-8263-4819-9acf-690e2e6fdd01
RRR_TRANSPORT=stackone python3 scripts/run_agent.py a34c2416-8263-4819-9acf-690e2e6fdd01 \
  --review-date 2026-11-25 --model claude-haiku-5-5
```

At StackOne consent, choose the project and existing LangCare account, grant only FHIR Search and FHIR Read, and leave **Load tools when needed** enabled. The local callback listens only on loopback during sign-in. The client stores the grant in `~/.config/rrr/stackone-oauth.json` with owner-only permissions and refreshes access as needed. If the grant expires or is revoked, run the login command again.

Run the single-search probe before a full review. It prints the count, verifies the returned patient references, and shows the action run ID and Defender status when supplied. Full reviews record the selected transport and Gateway action metadata in the trace. Seat billing must be checked separately in StackOne. Direct LangCare remains the default and can be selected explicitly with `RRR_TRANSPORT=langcare`. The deployed worker continues to use its existing direct connection.

## Operations dashboard

The Django dashboard has administrator sign-in and pages for run configuration, reports, and history. The configuration page reads patient IDs from the ignored `local-config/live-fixture-ids.json` map. Set `RRR_FIXTURE_MAP` to use a different copy. The default model options were verified against the existing Claude connection. `RRR_CLAUDE_MODELS` can select a comma-separated list of other verified IDs.

Use Python 3.12 and a local virtual environment. Set a private `DJANGO_SECRET_KEY` for this environment. Use `DJANGO_DEBUG=1` only for local HTTP development.

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r dashboard/requirements.txt
export DJANGO_DEBUG=1
export DJANGO_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver 127.0.0.1:8000
```

In a second shell with the same environment and SQLite path, start the review worker.

```sh
python manage.py run_review_worker
```

Keep the same secret across restarts when you want existing sessions to stay valid. SQLite defaults to the ignored `db.sqlite3` file. Override that location with `DJANGO_DB_PATH`. Run `python manage.py test dashboard` to check sign-in, CSRF, page visibility, and fixture and model choices.

A web request validates the synthetic patient, model, and date, then queues a run in SQLite. A separate worker claims it with a conditional update and invokes the existing agent CLI with the selected model. The browser polls the real agent trace every two seconds. One worker is suitable for this demo. An interrupted run becomes a visible failure after its time limit so it can be retried explicitly.

SQLite stores the application audit record and the validated report. The separate technical trace stores stages, timed tool activity, and errors. Evidence cards display short verified excerpts and measurements. Administrators can inspect failed comparisons in the run detail timeline. The progress endpoint returns only the validation summary.

### Deployment

The canonical dashboard is [referralsagent.garwalsh.com](https://referralsagent.garwalsh.com/).
OpenEMR has a browser-facing address at [emr.garwalsh.com](https://emr.garwalsh.com/).
The [legacy OpenEMR address](https://rrr.garwalsh.com/) remains active for FHIR and OAuth.
The LangCare MCP endpoint is `https://mcp.rrr.garwalsh.com/mcp`. See the [deployment notes](deploy/README.md).

The [Compose configuration](deploy/compose.yaml) runs Gunicorn with WhiteNoise for static assets and a separately supervised worker from the same image. Both use the same persistent SQLite and trace directory. The services attach to the existing `rrr_default` network. Caddy routes the dashboard hostname to `rrr-dashboard:8000` over that network.

Keep `dashboard.env` and `runner.env` outside the checkout and Docker build context. Only the worker needs the Claude and LangCare credentials. Set `DJANGO_ALLOWED_HOSTS` to the dashboard hostname, set `DJANGO_CSRF_TRUSTED_ORIGINS` to its HTTPS origin, and enable `DJANGO_TRUST_PROXY=1` behind Caddy. Set `DJANGO_USE_WHITENOISE=1`, `DJANGO_DB_PATH`, `RRR_RUN_DIR`, and `RRR_FIXTURE_MAP` to the deployed static and persistent paths. Use Django's `migrate` and `createsuperuser` commands to initialize the server.

This dashboard demonstrates audit-oriented engineering for synthetic data and makes no claim of HIPAA compliance. Production would require additional security, access governance, retention policies, and operational controls.


## Demo documentation checklist

1. **Referral reason.** Palpitations are documented as the reason for the proposed referral.
2. **Symptom history.** Frequency, duration, and associated symptoms are documented.
3. **Physical assessment.** Blood pressure, heart rate, and a cardiovascular examination are documented.
4. **ECG.** A 12-lead ECG report is documented within the preceding 90 days, a threshold set for this demo.
5. **Relevant history.** Patient-reported current medications, cardiac history, and family history are documented.

For v1, medication history is assessed from clinical notes exposed as FHIR `DiagnosticReport` resources. An explicit statement that the patient takes no medications satisfies the medication component. An empty list or a note that omits medication history provides no evidence about medication use. The review is limited to documentation of patient-reported history. The design uses the existing read-only OAuth scopes and omits `MedicationRequest` and `MedicationStatement` queries.

## Synthetic records and evaluation

Two existing synthetic patients serve as integration smoke tests. Both have a November 20, 2026 consultation and November 23, 2026 ECG documentation. The fixed review date is November 25, 2026.

| FHIR Patient ID | Scenario | Expected status |
| --- | --- | --- |
| `a349d267-f0bb-41a3-ba33-902d9b2fad5c` | Complete documentation | `READY` |
| `a34bf068-1fc2-429e-8be9-98aa34689c84` | Missing patient-reported medication history | `NEEDS_REVIEW` |

Run `python3 scripts/generate_fixtures.py` with Python 3.9 or newer. The generator uses a fixed seed and stable resource IDs to produce nine synthetic cases, including incomplete, outdated, and distributed evidence. The clinical bundle in `evals/fixtures/rrr-v1.json` contains only the evidence. The answer key in `evals/expected/rrr-v1.json` contains the scenario labels, checklist results, and expected statuses. Give the agent access only to the clinical evidence.

Identical reruns reuse the two files. If an output differs, the generator stops before replacing it. Use `--output-dir` to generate into a fresh directory. The generator operates offline and never accesses OpenEMR.

### Rehearse an OpenEMR import

Use an OpenEMR 8.5.0 copy restored from a consistent backup. The loader requires a MariaDB server with TCP networking disabled and both PHP database drivers pointed at that copy's Unix socket. The restored OpenEMR site's database host must be `localhost`. Provide an active user in the restored site as the fixture author. The loader uses OpenEMR's local services for patient, encounter, note, and measured vital sign writes. No OAuth scope changes are needed.

Preview the complete case without connecting to OpenEMR.

```sh
php scripts/load_fixture.php --fixture=4f07e0b9-b4e9-5226-957d-509c53f04eb8
```

Import the same case twice, using the restored application's PHP runtime.

```sh
php -d "mysqli.default_socket=$RESTORE_SOCKET" \
    -d "pdo_mysql.default_socket=$RESTORE_SOCKET" \
    scripts/load_fixture.php --openemr-root="$RESTORE_OPENEMR_ROOT" \
    --socket="$RESTORE_SOCKET" --author="$RESTORE_AUTHOR" \
    --fixture=4f07e0b9-b4e9-5226-957d-509c53f04eb8 --apply
```

Expect `created` on the first run and `reused` with the same FHIR IDs on the second. The loader checks the chart and reads the notes and measured vitals through OpenEMR's FHIR services. It refuses protected patient IDs, duplicate fixture identifiers, and existing charts that differ from the fixture. The answer key is separate from the loader and its read-back.

The loader groups the clinical writes in a transaction. Keep the original backup until the rehearsal passes. If read-back fails after commit or a partial chart is detected, restore a fresh disposable copy from the original backup before retrying. On a copy that contains the two original smoke-test patients, verify both charts before and after import. A local FHIR service check covers resource mapping. An end-to-end StackOne check also requires a LangCare connection to the restored instance.

### Run the nine-case evaluation

Install the agent requirements and configure the same environment as for a synthetic review. Keep a local `local-config/live-fixture-ids.json` mapping the generated fixture UUIDs to their loaded FHIR patient IDs.

```sh
python3 -m unittest discover -s scripts -p 'test_evaluate.py'
python3 scripts/evaluate.py
```

The evaluator joins the separate answer key to the live ID map and calls the existing agent CLI once per case with `claude-haiku-5-5` and review date `2026-11-25`. The agent receives the FHIR ID, model, date and trace settings. The evaluator retains the expected checklist and readiness values. Each invocation has a five-minute timeout. A failed invocation is recorded and evaluation continues with the next fixture.

A strict pass requires an exact match on all five checklist items and overall readiness, a schema-valid report, verified citations, and a completed run without unresolved errors or guardrail violations. Citation verification combines the runner's evidence validation with a check that every reported citation appears in a successful patient-scoped retrieval in that run. The per-citation metric checks reference matching. The per-case citation gate also uses the runner's evidence-validation and trace checks. The requirement and readiness rates report label agreement independently of strict passes, with absent answers counted as incorrect. Tool calls count dispatched requests. The JSON also records attempts the runner rejected before dispatch.

Each execution creates a new private run directory under the Git-ignored `evals/output/`, with `summary.md`, `results.json` and per-case traces and reports where available. The JSON captures the exact system instructions, prompt and source hashes, Git HEAD, working-tree status for the agent source files and dataset identifiers. The system prompt hash identifies the instructions sent to Claude. Source hashes also capture the code-side policy and output schema. The evaluator runs independently of Django.

### Inspect evaluations in the dashboard

Sign in and open **Evaluations** to browse completed runs, inspect checklist mismatches and recorded evidence, and see the original system instructions and prompt hash. Select two completed evaluations of the same dataset to compare metrics and fixture outcomes and view the line-based prompt diff.

The dashboard reads local `evals/output/` by default. Override the root with `RRR_EVAL_DIR`. It reads the evaluator's existing artifact layout without importing results into SQLite. See the [deployment notes](deploy/README.md) for transferring evaluation artifacts.

See [AGENTS.md](AGENTS.md) for project boundaries and verified FHIR behavior.
