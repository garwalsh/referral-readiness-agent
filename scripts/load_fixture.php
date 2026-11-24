<?php

declare(strict_types=1);

use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Session\SessionUtil;
use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\Services\ClinicalNotesService;
use OpenEMR\Services\EncounterService;
use OpenEMR\Services\FHIR\FhirDiagnosticReportService;
use OpenEMR\Services\FHIR\FhirObservationService;
use OpenEMR\Services\FHIR\Subscriber\CalculatedObservationEventsSubscriber;
use OpenEMR\Events\Services\ServiceSaveEvent;
use OpenEMR\Services\PatientService;
use OpenEMR\Services\VitalsService;
use OpenEMR\Validators\ProcessingResult;

const SMOKE_TEST_PATIENTS = [
    'a349d267-f0bb-41a3-ba33-902d9b2fad5c',
    'a34bf068-1fc2-429e-8be9-98aa34689c84',
];
const IDENTIFIER_SYSTEM = 'urn:rrr:referral-readiness-agent:fixtures';
const NOTE_TYPES = ['11506-3' => 'progress_note', '28570-0' => 'procedure_note'];

function requireCondition(bool $condition, string $message): void
{
    if (!$condition) {
        throw new RuntimeException($message);
    }
}

function clinicalPlan(string $fixtureId): array
{
    $path = __DIR__ . '/../evals/fixtures/rrr-v1.json';
    $bundle = json_decode((string) file_get_contents($path), true, flags: JSON_THROW_ON_ERROR);
    $subject = "urn:uuid:$fixtureId";
    $patient = null;
    $visits = [];
    foreach ($bundle['entry'] as ['fullUrl' => $url, 'resource' => $resource]) {
        if ($url === $subject && $resource['resourceType'] === 'Patient') {
            requireCondition($patient === null, 'Duplicate fixture patient ID.');
            $patient = $resource;
        }
        if (($resource['subject']['reference'] ?? null) === $subject && $resource['resourceType'] === 'Encounter') {
            $date = $resource['period']['start'];
            requireCondition(!isset($visits[$url]), 'Duplicate fixture encounter ID.');
            $visits[$url] = ['date' => $date, 'notes' => [], 'vitals' => []];
        }
    }
    requireCondition(
        $patient !== null && $patient['identifier'] === [['system' => IDENTIFIER_SYSTEM, 'value' => $fixtureId]],
        'Patient ID does not belong to the generated fixture bundle.'
    );
    requireCondition($visits !== [], 'Fixture patient has no encounters.');

    foreach ($bundle['entry'] as ['resource' => $resource]) {
        if (($resource['subject']['reference'] ?? null) !== $subject || $resource['resourceType'] === 'Encounter') {
            continue;
        }
        $visit = $resource['encounter']['reference'];
        requireCondition(isset($visits[$visit]), 'Fixture resource references an unknown encounter.');
        requireCondition($resource['effectiveDateTime'] === $visits[$visit]['date'], 'Fixture resource date differs from its encounter.');
        $coding = $resource['code']['coding'][0];
        requireCondition($coding['system'] === 'http://loinc.org', 'Expected a LOINC-coded fixture.');
        $code = $coding['code'];
        if ($resource['resourceType'] === 'DiagnosticReport') {
            $attachment = $resource['presentedForm'][0];
            $narrative = base64_decode($attachment['data'], true);
            requireCondition(
                isset(NOTE_TYPES[$code]) && $attachment['contentType'] === 'text/plain' && $narrative !== false && $narrative !== '',
                'Unsupported clinical note.'
            );
            requireCondition(!isset($visits[$visit]['notes'][$code]), 'Duplicate note type for this encounter.');
            $visits[$visit]['notes'][$code] = ['title' => $coding['display'], 'text' => $narrative];
        } elseif ($resource['resourceType'] === 'Observation' && $code === '85354-9') {
            $componentCodes = array_column(array_column(array_column($resource['component'], 'code'), 'coding'), 0);
            requireCondition(array_column($componentCodes, 'code') === ['8480-6', '8462-4'], 'Unexpected blood pressure components.');
            foreach ($resource['component'] as $index => $component) {
                $field = ['bps', 'bpd'][$index];
                requireCondition(!isset($visits[$visit]['vitals'][$field]), 'Duplicate blood pressure component.');
                requireCondition($component['valueQuantity']['code'] === 'mm[Hg]', 'Unexpected blood pressure unit.');
                $visits[$visit]['vitals'][$field] = (int) $component['valueQuantity']['value'];
            }
        } elseif ($resource['resourceType'] === 'Observation' && $code === '8867-4') {
            requireCondition(!isset($visits[$visit]['vitals']['pulse']), 'Duplicate heart rate.');
            requireCondition($resource['valueQuantity']['code'] === '/min', 'Unexpected heart rate unit.');
            $visits[$visit]['vitals']['pulse'] = (int) $resource['valueQuantity']['value'];
        } else {
            throw new RuntimeException('Unsupported fixture resource.');
        }
    }
    requireCondition(count(array_unique(array_column($visits, 'date'))) === count($visits), 'Fixture encounters must have distinct dates.');
    return ['birth_date' => $patient['birthDate'], 'visits' => array_column($visits, null, 'date')];
}

function firstResult(ProcessingResult $result, string $operation): array
{
    requireCondition(!$result->hasErrors() && $result->hasData(), "$operation did not succeed.");
    return $result->getFirstDataResult();
}

function writeChart(
    array $case,
    array $identity,
    array $author,
    PatientService $patientService,
    EncounterService $encounterService,
    ClinicalNotesService $noteService,
    VitalsService $vitalsService
): void {
    $patient = firstResult($patientService->insert($identity), 'Patient creation');
    $pid = (int) $patient['pid'];
    $patientUuid = $patient['uuid'];
    requireCondition(!in_array($patientUuid, SMOKE_TEST_PATIENTS, true), 'Refusing to write to a protected patient.');
    $category = QueryUtils::fetchSingleValue(
        'SELECT pc_catid FROM openemr_postcalendar_categories WHERE pc_catname = ? ORDER BY pc_catid LIMIT 1',
        'pc_catid',
        ['Office Visit']
    );
    requireCondition(is_numeric($category), 'OpenEMR Office Visit category is unavailable.');

    foreach ($case['visits'] as $visit) {
        $timestamp = $visit['date'] . ' 12:00:00';
        $encounter = firstResult($encounterService->insertEncounter($patientUuid, [
            'date' => $timestamp,
            'class_code' => 'AMB',
            'pc_catid' => $category,
            'provider_id' => (int) $author['id'],
            'user' => $author['username'],
            'group' => $author['facility'],
        ]), 'Encounter creation');
        $eid = (int) $encounter['encounter'];
        if ($visit['notes'] !== []) {
            $formId = $noteService->createClinicalNotesParentForm($pid, $eid, 1);
            foreach ($visit['notes'] as $code => $note) {
                $saved = $noteService->saveArray([
                    'form_id' => $formId, 'pid' => $pid, 'encounter' => $eid,
                    'code' => "LOINC:$code", 'codetext' => $note['title'], 'description' => $note['text'],
                    'clinical_notes_type' => NOTE_TYPES[$code], 'clinical_notes_category' => 'cardiology',
                    'authorized' => 1, 'activity' => ClinicalNotesService::ACTIVITY_ACTIVE,
                    'date' => $visit['date'], 'user' => $author['username'], 'groupname' => $author['facility'],
                ]);
                requireCondition(!empty($saved['id']), 'Clinical note creation did not return an ID.');
            }
        }
        if ($visit['vitals'] !== []) {
            $vitalsService->saveVitalsArray($visit['vitals'] + [
                'pid' => $pid, 'eid' => $eid, 'authorized' => 1, 'date' => $timestamp, 'activity' => 1,
            ]);
        }
    }
}

function verifyChart(array $patient, array $identity, array $case): array
{
    $pid = (int) $patient['pid'];
    $uuid = UuidRegistry::uuidToString($patient['uuid']);
    requireCondition(!in_array($uuid, SMOKE_TEST_PATIENTS, true), 'Fixture identifier matches a protected patient.');
    foreach ($identity as $field => $value) {
        requireCondition($patient[$field] === $value, 'Existing fixture demographics differ. No changes were made to this chart.');
    }

    $actual = QueryUtils::fetchRecords(
        'SELECT encounter, uuid, DATE(date) AS visit_date FROM form_encounter WHERE pid = ?',
        [$pid]
    );
    requireCondition(count($actual) === count($case['visits']), 'Encounter count differs from the fixture.');
    $encounters = array_column($actual, null, 'visit_date');
    requireCondition(count($encounters) === count($actual), 'Multiple encounters share a fixture date.');

    $reportResult = (new FhirDiagnosticReportService())->getAll(['patient' => $uuid], $uuid);
    $vitalResult = (new FhirObservationService())->getAll(['patient' => $uuid, 'category' => 'vital-signs'], $uuid);
    requireCondition(!$reportResult->hasErrors() && !$vitalResult->hasErrors(), 'OpenEMR FHIR read-back failed.');
    $reports = json_decode(json_encode($reportResult->getData(), JSON_THROW_ON_ERROR), true, flags: JSON_THROW_ON_ERROR);
    $observations = json_decode(json_encode($vitalResult->getData(), JSON_THROW_ON_ERROR), true, flags: JSON_THROW_ON_ERROR);
    $result = ['patient' => $uuid, 'encounters' => [], 'diagnostic_reports' => [], 'observations' => []];

    foreach ($case['visits'] as $date => $visit) {
        requireCondition(isset($encounters[$date]), 'An expected encounter date is missing.');
        $encounter = $encounters[$date];
        $eid = UuidRegistry::uuidToString($encounter['uuid']);
        $result['encounters'][$date] = $eid;
        $forms = QueryUtils::fetchRecords(
            'SELECT formdir FROM forms WHERE pid = ? AND encounter = ? AND deleted = 0 ORDER BY formdir',
            [$pid, $encounter['encounter']]
        );
        $expectedForms = ['newpatient'];
        if ($visit['notes'] !== []) {
            array_unshift($expectedForms, 'clinical_notes');
        }
        if ($visit['vitals'] !== []) {
            $expectedForms[] = 'vitals';
        }
        requireCondition(array_column($forms, 'formdir') === $expectedForms, 'Encounter form count or type differs.');

        $notes = array_filter($reports, fn ($report) => str_ends_with($report['encounter']['reference'] ?? '', "/$eid"));
        requireCondition(count($notes) === count($visit['notes']), 'FHIR clinical note count differs.');
        foreach ($notes as $note) {
            $code = $note['code']['coding'][0]['code'];
            requireCondition(isset($visit['notes'][$code]), 'Unexpected or duplicate FHIR clinical note.');
            $attachment = array_values(array_filter(
                $note['presentedForm'] ?? [],
                fn ($form) => $form['contentType'] === 'text/plain'
            ))[0] ?? [];
            requireCondition(
                base64_decode($attachment['data'] ?? '', true) === $visit['notes'][$code]['text'],
                'FHIR note narrative differs from the fixture.'
            );
            $result['diagnostic_reports'][$date][$code] = $note['id'];
            unset($visit['notes'][$code]);
        }

        $vitals = array_filter($observations, fn ($observation) => str_ends_with($observation['encounter']['reference'] ?? '', "/$eid"));
        foreach ($vitals as $observation) {
            $code = $observation['code']['coding'][0]['code'];
            if ($code === '85354-9') {
                $readings = [];
                foreach ($observation['component'] ?? [] as $component) {
                    $readings[$component['code']['coding'][0]['code']] = $component['valueQuantity']['value'] ?? null;
                }
                if (isset($visit['vitals']['bps'], $visit['vitals']['bpd'])) {
                    requireCondition(
                        ($readings['8480-6'] ?? null) == $visit['vitals']['bps'] && ($readings['8462-4'] ?? null) == $visit['vitals']['bpd'],
                        'FHIR blood pressure differs from the fixture.'
                    );
                } else {
                    continue;
                }
            } elseif ($code === '8867-4' && isset($visit['vitals']['pulse'])) {
                requireCondition(
                    ($observation['valueQuantity']['value'] ?? null) == $visit['vitals']['pulse'],
                    'FHIR heart rate differs from the fixture.'
                );
            } else {
                continue;
            }
            requireCondition(!isset($result['observations'][$date][$code]), 'Duplicate FHIR vital sign.');
            $result['observations'][$date][$code] = $observation['id'];
        }
        if ($visit['vitals'] !== []) {
            requireCondition(count($result['observations'][$date] ?? []) === 2, 'Expected FHIR blood pressure and heart rate.');
        }
    }
    requireCondition(count($reports) === array_sum(array_map('count', $result['diagnostic_reports'])), 'FHIR notes exist outside the expected encounters.');
    return $result;
}

try {
    requireCondition(PHP_SAPI === 'cli', 'Run this script with PHP CLI.');
    $options = getopt('', ['openemr-root:', 'socket:', 'fixture:', 'author:', 'apply', 'help']);
    if (isset($options['help'])) {
        echo "Usage: php scripts/load_fixture.php --openemr-root=PATH --socket=PATH --fixture=UUID --author=USERNAME [--apply]\n";
        exit(0);
    }
    $fixtureId = $options['fixture'] ?? '';
    requireCondition(
        is_string($fixtureId) && preg_match('/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/D', $fixtureId) === 1
            && !in_array($fixtureId, SMOKE_TEST_PATIENTS, true),
        'Supply a generated fixture patient UUID with --fixture.'
    );
    $case = clinicalPlan($fixtureId);
    if (!isset($options['apply'])) {
        echo json_encode(['fixture' => $fixtureId, 'encounters' => array_keys($case['visits']), 'mode' => 'plan'], JSON_THROW_ON_ERROR) . "\n";
        exit(0);
    }

    $root = realpath($options['openemr-root'] ?? '');
    $socket = realpath($options['socket'] ?? '');
    requireCondition(
        $root !== false && $socket !== false && filetype($socket) === 'socket' && !empty($options['author']),
        'Apply requires an OpenEMR root, isolated MariaDB socket, and local OpenEMR author.'
    );
    $configPath = "$root/sites/default/sqlconf.php";
    $hostName = (static function (string $configPath): string {
        require $configPath;
        return $host ?? '';
    })($configPath);
    requireCondition($hostName === 'localhost', 'The restored site must connect via a local Unix socket.');
    foreach (['mysqli.default_socket', 'pdo_mysql.default_socket'] as $setting) {
        requireCondition(
            realpath(ini_get($setting) ?: '.') === $socket,
            "Start PHP with -d $setting=<restored socket>."
        );
    }
    $_GET['site'] = 'default';
    $ignoreAuth = true;
    $sessionAllowWrite = true;
    require "$root/interface/globals.php";
    require_once "$root/library/forms.inc.php";
    $connection = QueryUtils::querySingleRow('SELECT @@socket AS socket_path, @@skip_networking AS no_tcp');
    requireCondition(
        realpath($connection['socket_path']) === $socket && (int) $connection['no_tcp'] === 1,
        'The connected database does not match the isolated, socket-only restore.'
    );
    $author = QueryUtils::querySingleRow(
        'SELECT id, username, facility FROM users WHERE username = ? AND active = 1',
        [$options['author']]
    );
    requireCondition(is_array($author) && (int) $author['id'] > 0, 'The restored site has no active user matching --author.');
    SessionUtil::setSession(['authUserID' => (int) $author['id'], 'authUser' => $author['username'], 'authProvider' => $author['facility']]);

    $identity = [
        'pubpid' => "rrr-v1-$fixtureId", 'DOB' => $case['birth_date'], 'sex' => 'unknown',
        'fname' => 'RRR', 'lname' => 'Fixture-' . substr($fixtureId, 0, 8),
    ];
    $lock = 'rrr-fixture-import';
    requireCondition((int) QueryUtils::fetchSingleValue('SELECT GET_LOCK(?, 10) AS acquired', 'acquired', [$lock]) === 1, 'Could not lock fixture import.');
    try {
        $findPatient = fn () => QueryUtils::fetchRecords(
            'SELECT pid, uuid, pubpid, fname, lname, DOB, sex FROM patient_data WHERE pubpid = ?',
            [$identity['pubpid']]
        );
        $patients = $findPatient();
        $created = $patients === [];
        if ($created) {
            // These constructors can start transactions while initializing UUIDs.
            $patientService = new PatientService();
            $encounterService = new EncounterService();
            $noteService = new ClinicalNotesService();
            $vitalsService = new VitalsService();
            $dispatcher = $vitalsService->getEventDispatcher();
            foreach ($dispatcher->getListeners(ServiceSaveEvent::EVENT_POST_SAVE) as $listener) {
                if (is_array($listener) && $listener[0] instanceof CalculatedObservationEventsSubscriber) {
                    // Fixtures contain measured vitals only. Keep the UUID mapping listener.
                    $dispatcher->removeListener(ServiceSaveEvent::EVENT_POST_SAVE, $listener);
                }
            }
            $patients = QueryUtils::inTransaction(function () use (
                $case, $identity, $author, $findPatient, $patientService, $encounterService, $noteService, $vitalsService
            ): array {
                writeChart($case, $identity, $author, $patientService, $encounterService, $noteService, $vitalsService);
                $patients = $findPatient();
                requireCondition(count($patients) === 1, 'Fixture identifier must match exactly one patient.');
                requireCondition(
                    (int) QueryUtils::fetchSingleValue('SELECT @@in_transaction AS active_tx', 'active_tx') === 1,
                    'OpenEMR ended the chart transaction. Restore this disposable database before retrying.'
                );
                return $patients;
            });
        }
        requireCondition(count($patients) === 1, 'Fixture identifier must match exactly one patient.');
        // FHIR readers may run their own UUID initialization transactions.
        $result = ['status' => $created ? 'created' : 'reused'] + verifyChart($patients[0], $identity, $case);
    } finally {
        QueryUtils::querySingleRow('SELECT RELEASE_LOCK(?)', [$lock]);
    }
    echo json_encode($result, JSON_THROW_ON_ERROR | JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES) . "\n";
} catch (Throwable $error) {
    fwrite(STDERR, $error->getMessage() . "\n");
    exit(1);
}
