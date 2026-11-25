# Referral Readiness Agent

This project checks whether a synthetic patient chart contains the documentation for a proposed cardiology referral for palpitations. It returns a checklist with citations and an overall documentation status. A `READY` result means the chart meets the demo checklist. Referral decisions stay with the care team.

## How it works

Claude reads the retrieved chart and drafts findings. Python limits the model to FHIR search and read calls, enforces patient scope on requests and returned resources, and caps tool attempts at ten. It checks citations against the records retrieved in that run, including verbatim note excerpts, and calculates readiness from the validated findings. The model still interprets whether the quoted evidence supports each checklist component.

On the hosted route, Python sends the approved tool calls through StackOne Gateway. StackOne uses a member OAuth grant to route requests to LangCare MCP and returns action IDs and Defender results when available. LangCare exposes OpenEMR's FHIR records as MCP tools. The hosted Django dashboard queues reviews in SQLite and a separate worker runs the same CLI. The hosted dashboard requires administrator sign-in.

The [demo checklist and fixture guide](docs/fixtures.md) explains the evidence requirements and how to load the synthetic charts.

## Run locally

Use Python 3.12, an Anthropic API key, and your own LangCare MCP connection to an OpenEMR instance containing synthetic charts.

```sh
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Set `ANTHROPIC_API_KEY`, `LANGCARE_MCP_URL`, and `LANGCARE_MCP_TOKEN` in the environment or the ignored project `.env`. Set `PATIENT_ID` in your shell to the FHIR ID of a loaded synthetic patient. The standalone runner defaults to direct LangCare.

```sh
python scripts/run_agent.py "$PATIENT_ID" --review-date 2026-11-25
```

Use `--model` to select a model available to your Anthropic account. The CLI prints the report as JSON and writes a JSONL execution trace under `logs/`. Failed citation comparisons can contain source excerpts, so keep traces private.

To use StackOne, see the [Gateway setup](deploy/README.md#stackone-gateway). For local Django setup and worker commands, see the [dashboard guide](deploy/README.md#run-the-dashboard-locally).

## Evaluation

The [fixture bundle](evals/fixtures/rrr-v1.json) contains nine generated charts. The [answer key](evals/expected/rrr-v1.json) is separate and stays in the evaluator. After loading the cases and creating the [local FHIR ID map](docs/fixtures.md#map-loaded-fixtures), run the evaluator with the same direct LangCare settings used above.

```sh
RRR_TRANSPORT=langcare python scripts/evaluate.py
```

The evaluator uses Claude Haiku 5.5 and the fixed review date of November 25, 2026. A strict pass requires all five checklist statuses and final readiness to match the key. It also requires a valid report, verified citations, and a run with no unresolved retrieval errors or execution, validation, or guardrail errors. Outputs under `evals/output/` retain the exact prompt, its hash, code provenance, and per-case traces. The dashboard can inspect and compare those files.

In the saved experiment, strict accuracy moved from **8/9 to 9/9** with the same model, date, and nine cases. The baseline treated a mention of palpitations as evidence of referral intent in one chart. The revised instruction requires explicit evidence that the proposed cardiology referral is for palpitations. That case passed the second time, and the other eight still passed. These were known fixtures and the revision addressed an observed failure. The result measures performance on this set.

## Check the code offline

```sh
python -m pip install -r dashboard/requirements.txt
python -m unittest discover -s scripts -p 'test_*.py'
DJANGO_SECRET_KEY=offline-test-only python manage.py test dashboard
```

Start with [the agent loop](scripts/run_agent.py), [report validation](scripts/reporting.py), or [the evaluator](scripts/evaluate.py). The [Django worker](dashboard/worker.py) launches the CLI and verifies the saved report and trace belong to the queued review.

## Limits

Use synthetic data only. Clinical decision support and referral approval are outside the demo’s scope. The deployment lacks the controls needed for HIPAA-compliant operation. A human must review the record and any proposed referral.

The nine cases give a small regression check. Citation validation verifies retrieved source references and excerpts, while clinical interpretation remains a model judgment. The StackOne profile was configured to select FHIR Search and Read. Independent testing of Gateway-side enforcement is still outstanding. The runner's own read-only allowlist is covered by offline tests.
