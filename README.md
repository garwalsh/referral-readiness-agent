# Referral Readiness Review (RRR) Agent

The Referral Readiness Review Agent is a technical portfolio project that uses synthetic electronic health records to assess documentation for a proposed cardiology referral for palpitations. A `READY` result indicates that the record satisfies the demo checklist. Clinical decisions remain with the care team.

## Current state

Patient search and clinical record retrieval have been tested end to end through **Claude → StackOne Gateway → LangCare MCP → OpenEMR FHIR API**. The clinical integration uses read-only FHIR access.

The standalone Python agent, evaluation harness, and fixture generator are planned. The agent will cite FHIR resource identifiers and relevant dates, distinguish missing documentation from evidence it could not retrieve, and limit findings to what the retrieved records support.

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

The planned `scripts/generate_fixtures.py` will use a fixed seed to generate synthetic cases with complete, incomplete, outdated, or distributed evidence. It will favor supported OpenEMR interfaces, preserve existing records, support repeatable loads without duplicates, and verify retrieval through the read-only FHIR path. Direct database loading would require prior validation of record relationships and FHIR exposure. Fixture definitions and reproduction instructions will be versioned, with expected outcomes stored separately from the clinical evidence available to the agent.

The planned harness will use deterministic Python checks for output schemas, expected statuses, and FHIR references. It will inspect retrieval and tool selection, record tool calls and errors in traces, and support failure analysis and version comparisons on held-out cases. Narrow semantic checks can help assess whether cited evidence supports a finding and flag unsupported claims for review.

See [AGENTS.md](AGENTS.md) for project boundaries and verified FHIR behavior.
