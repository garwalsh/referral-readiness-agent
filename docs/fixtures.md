# Synthetic fixtures

The demo reviews five documentation requirements for a proposed cardiology referral for palpitations.

1. **Referral reason** means the record explicitly connects palpitations to the proposed referral.
2. **Symptom history** includes frequency, duration, and associated symptoms.
3. **Physical assessment** includes blood pressure, heart rate, and a cardiovascular exam.
4. **ECG** requires a documented 12-lead ECG within the previous 90 days, including day 90. This window is a demo rule.
5. **Relevant history** includes patient-reported current medications, cardiac history, and family history. An explicit statement that the patient takes no medications counts as evidence for the medication component.

The tested OpenEMR 8.5.0 setup exposes progress and procedure notes as `DiagnosticReport` resources with plain-text `presentedForm` attachments, and measured vitals as `Observation`. The agent identifies ECG documents from the report text. Medication history comes from notes. The integration does not query medication endpoints.

## Generate the dataset

```sh
python scripts/generate_fixtures.py
```

The generator uses a fixed seed and stable IDs. It reuses byte-identical output and refuses to overwrite changed files. Use `--output-dir` to generate into a fresh directory. The generator runs offline and separates the chart evidence from the expected outcomes.

## Rehearse an import

Use a disposable OpenEMR 8.5.0 restore and an active OpenEMR user as the fixture author. The loader requires a local Unix socket, `localhost` in the restored site's configuration, and MariaDB TCP networking disabled. It writes through OpenEMR's local services. The agent uses a separate read-only FHIR connection.

Set `FIXTURE_ID` to a generated patient UUID from the bundle. Preview it without an OpenEMR connection.

```sh
php scripts/load_fixture.php --fixture="$FIXTURE_ID"
```

Set `RESTORE_SOCKET`, `RESTORE_OPENEMR_ROOT`, and `RESTORE_AUTHOR` for the disposable restore, then use its PHP runtime.

```sh
php -d "mysqli.default_socket=$RESTORE_SOCKET" \
    -d "pdo_mysql.default_socket=$RESTORE_SOCKET" \
    scripts/load_fixture.php --openemr-root="$RESTORE_OPENEMR_ROOT" \
    --socket="$RESTORE_SOCKET" --author="$RESTORE_AUTHOR" \
    --fixture="$FIXTURE_ID" --apply
```

An initial import should report `created`. An identical rerun should report `reused` with the same FHIR IDs. The script checks the chart and verifies notes and measured vitals through OpenEMR's FHIR services. It rejects protected IDs, duplicates, and mismatched charts. Keep the original backup until the rehearsal passes. If read-back fails after commit or a partial chart is detected, restore a fresh copy before retrying.

## Map loaded fixtures

Create the ignored file `local-config/live-fixture-ids.json` with one entry per imported fixture. The key is the generated fixture UUID, and `patient` is the FHIR patient UUID from the loader output.

```json
{
  "00000000-0000-4000-8000-000000000001": {
    "patient": "00000000-0000-4000-8000-000000000002"
  }
}
```

Replace both example UUIDs with values from your own synthetic install. The evaluator requires all nine distinct fixture-to-patient mappings. The dashboard can use any nonempty synthetic allowlist in the same shape, with `RRR_FIXTURE_MAP` available to point it at another file.
