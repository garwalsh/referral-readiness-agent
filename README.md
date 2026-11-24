# Referral Readiness Review (RRR) Agent

The Referral Readiness Review Agent is a technical portfolio project that uses synthetic electronic health records to assess documentation for a proposed cardiology referral for palpitations. A `READY` result indicates that the record satisfies the demo checklist. Clinical decisions remain with the care team.

## Current state

Patient search and clinical record retrieval have been tested end to end through **Claude → StackOne Gateway → LangCare MCP → OpenEMR FHIR API**. The clinical integration uses read-only FHIR access.

The standalone Python agent uses **Claude → LangCare MCP → OpenEMR FHIR API**. It returns a validated JSON report with one finding per checklist item. The offline fixture generator and a one-case loader for an isolated OpenEMR restore are available. The evaluation harness is planned.

## Run a synthetic review

Install the reporting dependency with `python3 -m pip install -r requirements.txt`. Set `ANTHROPIC_API_KEY`, `LANGCARE_MCP_URL`, and `LANGCARE_MCP_TOKEN` in the environment or the Git-ignored `.env`. The runner defaults to Haiku 5.5. Set `--model` to compare models on the same patient and review date.

```sh
python3 scripts/run_agent.py a34c2416-8263-4819-9acf-690e2e6fdd01 \
  --review-date 2026-11-25
```

The prompt asks Claude to treat clinical narratives as untrusted evidence, interpret the checklist, and propose supporting IDs and note excerpts. Python enforces patient scope, the read-only tool allowlist, and the ten-attempt cap. It validates all five findings, verifies each citation and excerpt against that run's retrieved chart, checks measured blood pressure and heart rate, and applies the inclusive 90-day ECG rule. Claude interprets which components the excerpts support. Python renders the explanations from checklist fields and calculates readiness.

A completed, unfiltered search can establish a documentation gap even if it returns zero results. Incomplete retrieval leaves unsupported findings unknown. The JSONL trace under `logs/` records run metadata, tool parameters and durations, retrieved IDs, validation failures, and final statuses. Full clinical narratives stay out of the trace.


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

The planned harness will use deterministic Python checks for output schemas, expected statuses, and FHIR references. It will inspect retrieval and tool selection, record tool calls and errors in traces, and support failure analysis and version comparisons on held-out cases. Narrow semantic checks can help assess whether cited evidence supports a finding and flag unsupported claims for review.

See [AGENTS.md](AGENTS.md) for project boundaries and verified FHIR behavior.
