-- Schema version 1. The data dictionary is docs/database/README.md.
-- Times are UTC, ISO 8601 text ("2026-11-22T10:02:31Z"). JSON columns hold JSON text.

CREATE TABLE roles (
    name        TEXT PRIMARY KEY,
    description TEXT NOT NULL
);
INSERT INTO roles VALUES
    ('programming', 'Master and engineers: users, programming variables, model approval, everything in Setup and Testing'),
    ('setup',       'Parts, failure modes, calibrated samples, training'),
    ('testing',     'Calibration check and testing of production parts');

CREATE TABLE users (
    id                   INTEGER PRIMARY KEY,
    username             TEXT NOT NULL UNIQUE COLLATE NOCASE CHECK (length(username) BETWEEN 1 AND 32),
    role                 TEXT NOT NULL REFERENCES roles(name),
    password_hash        TEXT NOT NULL,
    must_change_password INTEGER NOT NULL DEFAULT 1 CHECK (must_change_password IN (0, 1)),
    failed_logins        INTEGER NOT NULL DEFAULT 0,
    locked_until_utc     TEXT,
    is_master            INTEGER NOT NULL DEFAULT 0 CHECK (is_master IN (0, 1)),
    active               INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_utc          TEXT NOT NULL,
    created_by           INTEGER REFERENCES users(id)
);
CREATE UNIQUE INDEX one_master ON users(is_master) WHERE is_master = 1;

CREATE TABLE sessions (
    token_sha256  TEXT PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    csrf_token    TEXT NOT NULL,
    created_utc   TEXT NOT NULL,
    last_seen_utc TEXT NOT NULL,
    ended_utc     TEXT
);

CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY,
    at_utc      TEXT NOT NULL,
    user_id     INTEGER REFERENCES users(id),
    action      TEXT NOT NULL,
    object_type TEXT,
    object_id   TEXT,
    detail      TEXT
);
CREATE INDEX audit_log_at ON audit_log(at_utc);

CREATE TABLE settings (
    key           TEXT PRIMARY KEY,
    value         TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('int', 'float', 'text', 'bool')),
    min_value     REAL,
    max_value     REAL,
    step          REAL,
    default_value TEXT NOT NULL,
    unit          TEXT,
    description   TEXT NOT NULL,
    updated_utc   TEXT,
    updated_by    INTEGER REFERENCES users(id)
);

CREATE TABLE stations (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    machine_code TEXT,
    created_utc  TEXT NOT NULL
);

CREATE TABLE shifts (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    start_local TEXT NOT NULL CHECK (start_local GLOB '[0-2][0-9]:[0-5][0-9]'),
    end_local   TEXT NOT NULL CHECK (end_local GLOB '[0-2][0-9]:[0-5][0-9]'),
    active      INTEGER NOT NULL DEFAULT 1
);

-- Who, which part and which shift, recorded when they change, not on every result row.
CREATE TABLE context_events (
    id         INTEGER PRIMARY KEY,
    at_utc     TEXT NOT NULL,
    station_id INTEGER REFERENCES stations(id),
    kind       TEXT NOT NULL CHECK (kind IN ('operator', 'part', 'shift')),
    value      TEXT NOT NULL,
    user_id    INTEGER REFERENCES users(id)
);
CREATE INDEX context_events_at ON context_events(station_id, at_utc);

CREATE TABLE sensors (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('microphone', 'accelerometer')),
    identity      TEXT NOT NULL UNIQUE,
    model         TEXT,
    adxl_range_g  REAL,
    adxl_odr_hz   REAL,
    adxl_axes     TEXT,
    notes         TEXT,
    installed_utc TEXT NOT NULL,
    removed_utc   TEXT
);

CREATE TABLE parts (
    id               INTEGER PRIMARY KEY,
    part_number      TEXT NOT NULL UNIQUE CHECK (length(part_number) BETWEEN 1 AND 20),
    description      TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 60),
    tach_connected   INTEGER NOT NULL DEFAULT 0 CHECK (tach_connected IN (0, 1)),
    tach_ppr         INTEGER CHECK (tach_ppr IS NULL OR tach_ppr > 0),
    capture_s        INTEGER NOT NULL DEFAULT 10 CHECK (capture_s BETWEEN 1 AND 60),
    adxl_enabled     INTEGER NOT NULL DEFAULT 0 CHECK (adxl_enabled IN (0, 1)),
    created_utc      TEXT NOT NULL,
    created_by       INTEGER REFERENCES users(id),
    archived_utc     TEXT,
    CHECK (tach_connected = 0 OR tach_ppr IS NOT NULL)
);

-- Optional order table: gear teeth, slot/pole counts, bearing orders.
CREATE TABLE part_orders (
    id          INTEGER PRIMARY KEY,
    part_id     INTEGER NOT NULL REFERENCES parts(id),
    name        TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 30),
    kind        TEXT NOT NULL CHECK (kind IN ('gear_teeth', 'slot_pole', 'bearing', 'other')),
    order_value REAL NOT NULL CHECK (order_value > 0),
    UNIQUE (part_id, name)
);

CREATE TABLE failure_modes (
    id          INTEGER PRIMARY KEY,
    part_id     INTEGER NOT NULL REFERENCES parts(id),
    name        TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 30),
    created_utc TEXT NOT NULL,
    created_by  INTEGER REFERENCES users(id),
    deleted_utc TEXT,
    deleted_by  INTEGER REFERENCES users(id)
);
CREATE UNIQUE INDEX failure_mode_name ON failure_modes(part_id, name) WHERE deleted_utc IS NULL;

-- Permanent raw recordings (calibrated samples only; files kept by the acquisition service).
CREATE TABLE recordings (
    id            TEXT PRIMARY KEY,
    path          TEXT NOT NULL,
    sha256        TEXT NOT NULL CHECK (length(sha256) = 64),
    bytes         INTEGER NOT NULL,
    seconds       REAL NOT NULL,
    clock_hz      INTEGER NOT NULL,
    sensor_id     INTEGER REFERENCES sensors(id),
    firmware      TEXT,
    filter_name   TEXT,
    filter_sha256 TEXT,
    purpose       TEXT NOT NULL CHECK (purpose IN ('golden', 'failure_mode', 'bench')),
    captured_utc  TEXT NOT NULL
);

CREATE TABLE samples (
    id              INTEGER PRIMARY KEY,
    part_id         INTEGER NOT NULL REFERENCES parts(id),
    kind            TEXT NOT NULL CHECK (kind IN ('golden', 'failure_mode')),
    failure_mode_id INTEGER REFERENCES failure_modes(id),
    sample_set      TEXT NOT NULL CHECK (sample_set IN ('training', 'validation')),
    unit_serial     TEXT CHECK (unit_serial IS NULL OR length(unit_serial) <= 40),
    recording_id    TEXT NOT NULL UNIQUE REFERENCES recordings(id),
    created_utc     TEXT NOT NULL,
    created_by      INTEGER REFERENCES users(id),
    deleted_utc     TEXT,
    deleted_by      INTEGER REFERENCES users(id),
    CHECK ((kind = 'golden') = (failure_mode_id IS NULL))
);
CREATE INDEX samples_part ON samples(part_id, kind, deleted_utc);

-- Captures that Setup threw away (Retest), logged as aborted.
CREATE TABLE aborted_captures (
    id          INTEGER PRIMARY KEY,
    at_utc      TEXT NOT NULL,
    part_id     INTEGER REFERENCES parts(id),
    target      TEXT NOT NULL,
    valid       INTEGER NOT NULL,
    reasons     TEXT,
    user_id     INTEGER REFERENCES users(id)
);

-- Analysed values: one row per owner and analysis set, values as a JSON object.
CREATE TABLE features (
    id               INTEGER PRIMARY KEY,
    owner_type       TEXT NOT NULL CHECK (owner_type IN ('sample', 'test_result')),
    owner_id         INTEGER NOT NULL,
    catalogue_version TEXT NOT NULL,
    filter_name      TEXT NOT NULL,
    filter_sha256    TEXT NOT NULL,
    gain             REAL,
    values_json      TEXT NOT NULL,
    created_utc      TEXT NOT NULL,
    UNIQUE (owner_type, owner_id)
);

CREATE TABLE reference_data (
    id          INTEGER PRIMARY KEY,
    part_id     INTEGER NOT NULL REFERENCES parts(id),
    kind        TEXT NOT NULL,
    value_json  TEXT NOT NULL,
    created_utc TEXT NOT NULL,
    created_by  INTEGER REFERENCES users(id)
);

CREATE TABLE models (
    id                 INTEGER PRIMARY KEY,
    part_id            INTEGER NOT NULL REFERENCES parts(id),
    method             TEXT NOT NULL,
    version            INTEGER NOT NULL,
    state              TEXT NOT NULL CHECK (state IN ('trained', 'approved', 'retired', 'invalidated')),
    feature_names_json TEXT NOT NULL,
    pruned_json        TEXT,
    params_json        TEXT,
    metrics_json       TEXT,
    artifact_path      TEXT,
    artifact_sha256    TEXT,
    trained_utc        TEXT NOT NULL,
    trained_by         INTEGER REFERENCES users(id),
    approved_utc       TEXT,
    approved_by        INTEGER REFERENCES users(id),
    UNIQUE (part_id, method, version)
);

-- The order in which model methods are consulted for a part (BIOS-boot-order style).
CREATE TABLE model_priority (
    part_id  INTEGER NOT NULL REFERENCES parts(id),
    position INTEGER NOT NULL,
    method   TEXT NOT NULL,
    enabled  INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    PRIMARY KEY (part_id, position),
    UNIQUE (part_id, method)
);

CREATE TABLE calibration_checks (
    id                INTEGER PRIMARY KEY,
    at_utc            TEXT NOT NULL,
    station_id        INTEGER REFERENCES stations(id),
    part_id           INTEGER NOT NULL REFERENCES parts(id),
    user_id           INTEGER REFERENCES users(id),
    golden_result_id  INTEGER,
    reject_result_id  INTEGER,
    passed            INTEGER NOT NULL CHECK (passed IN (0, 1)),
    gain              REAL,
    detail_json       TEXT
);

CREATE TABLE test_results (
    id                   INTEGER PRIMARY KEY,
    at_utc               TEXT NOT NULL,
    station_id           INTEGER REFERENCES stations(id),
    part_id              INTEGER NOT NULL REFERENCES parts(id),
    unit_serial          TEXT,
    purpose              TEXT NOT NULL DEFAULT 'production' CHECK (purpose IN ('production', 'calibration_golden', 'calibration_reject')),
    verdict              TEXT NOT NULL CHECK (verdict IN ('OK', 'NOK', 'UNDEFINED', 'INVALID')),
    failure_mode_id      INTEGER REFERENCES failure_modes(id),
    drift                INTEGER NOT NULL DEFAULT 0 CHECK (drift IN (0, 1)),
    invalid_reasons      TEXT,
    decision_json        TEXT,
    gain                 REAL,
    retest_of            INTEGER REFERENCES test_results(id),
    user_id              INTEGER REFERENCES users(id),
    calibration_check_id INTEGER REFERENCES calibration_checks(id),
    CHECK (verdict = 'NOK' OR failure_mode_id IS NULL)
);
CREATE INDEX test_results_at ON test_results(at_utc);
CREATE INDEX test_results_part ON test_results(part_id, at_utc);

CREATE TABLE drift_events (
    id               INTEGER PRIMARY KEY,
    at_utc           TEXT NOT NULL,
    part_id          INTEGER NOT NULL REFERENCES parts(id),
    test_result_id   INTEGER REFERENCES test_results(id),
    feature          TEXT NOT NULL,
    rule             TEXT NOT NULL,
    value            REAL,
    limit_value      REAL,
    acknowledged_utc TEXT,
    acknowledged_by  INTEGER REFERENCES users(id)
);

CREATE TABLE io_events (
    id          INTEGER PRIMARY KEY,
    at_utc      TEXT NOT NULL,
    station_id  INTEGER REFERENCES stations(id),
    kind        TEXT NOT NULL,
    detail_json TEXT
);

-- Store-and-forward queue for a future edge server (MQTT / OPC UA / REST).
CREATE TABLE edge_outbox (
    id           INTEGER PRIMARY KEY,
    created_utc  TEXT NOT NULL,
    topic        TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    sent_utc     TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT
);
CREATE INDEX edge_outbox_unsent ON edge_outbox(sent_utc) WHERE sent_utc IS NULL;
