"""Transactional, explicit state database upgrades."""
VERSION_ONE = r"""
                CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
                INSERT OR IGNORE INTO schema_version VALUES (1);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, mode TEXT NOT NULL CHECK(mode='fixture'),
                    input TEXT NOT NULL, input_digest TEXT NOT NULL,
                    state TEXT NOT NULL, version INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    stage TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL,
                    UNIQUE(run_id,stage));
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    number INTEGER NOT NULL, token TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL, started_at TEXT, ended_at TEXT,
                    error TEXT, evidence TEXT, UNIQUE(run_id,number));
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
                    version INTEGER NOT NULL, type TEXT NOT NULL, time TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id,id);
                CREATE UNIQUE INDEX IF NOT EXISTS events_version ON events(run_id,version);
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    attempt_id TEXT NOT NULL REFERENCES attempts(id), media_type TEXT NOT NULL,
                    size INTEGER NOT NULL, sha256 TEXT NOT NULL, storage_ref TEXT NOT NULL UNIQUE);
                CREATE TABLE IF NOT EXISTS idempotency (
                    scope TEXT NOT NULL, key TEXT NOT NULL, digest TEXT NOT NULL,
                    run_id TEXT REFERENCES runs(id), PRIMARY KEY(scope,key));
"""

VERSION_TWO = r"""
CREATE TABLE runs_next (id TEXT PRIMARY KEY, mode TEXT NOT NULL CHECK(mode IN ('fixture','mpt')),
 input TEXT NOT NULL,input_digest TEXT NOT NULL,state TEXT NOT NULL,version INTEGER NOT NULL,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
INSERT INTO runs_next SELECT * FROM runs;
DROP TABLE runs;
ALTER TABLE runs_next RENAME TO runs;
CREATE TABLE components (id TEXT PRIMARY KEY, manifest TEXT NOT NULL);
CREATE TABLE contents (id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES runs(id));
CREATE TABLE media (artifact_id TEXT PRIMARY KEY REFERENCES artifacts(id), content_id TEXT NOT NULL REFERENCES contents(id),
 parent_artifact_id TEXT REFERENCES media(artifact_id), metadata TEXT NOT NULL);
CREATE TABLE generation_context (run_id TEXT PRIMARY KEY REFERENCES runs(id),component_id TEXT NOT NULL REFERENCES components(id));
CREATE TABLE publish_intents (id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id),
 content_id TEXT NOT NULL REFERENCES contents(id),platform TEXT NOT NULL,account TEXT NOT NULL,
 state TEXT NOT NULL,version INTEGER NOT NULL,created_at TEXT NOT NULL,
 UNIQUE(platform,account,content_id));
CREATE TABLE publish_attempts (id TEXT PRIMARY KEY,intent_id TEXT NOT NULL REFERENCES publish_intents(id),
 token TEXT NOT NULL UNIQUE,state TEXT NOT NULL,lease_until REAL NOT NULL,started_at TEXT NOT NULL,ended_at TEXT);
CREATE TABLE account_locks (platform TEXT NOT NULL,account TEXT NOT NULL,token TEXT NOT NULL UNIQUE,
 PRIMARY KEY(platform,account));
CREATE TABLE receipts (id TEXT PRIMARY KEY,intent_id TEXT NOT NULL REFERENCES publish_intents(id),
 attempt_id TEXT REFERENCES publish_attempts(id),created_at TEXT NOT NULL,source TEXT NOT NULL,
 outcome TEXT NOT NULL,work_id TEXT,evidence TEXT NOT NULL);
CREATE TABLE publish_requests (scope TEXT NOT NULL,key TEXT NOT NULL,digest TEXT NOT NULL,
 intent_id TEXT NOT NULL REFERENCES publish_intents(id),PRIMARY KEY(scope,key));
DELETE FROM schema_version;
INSERT INTO schema_version VALUES(2);
"""

VERSION_THREE = r"""
ALTER TABLE runs ADD COLUMN trace_id TEXT;
CREATE TABLE trace_links (id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES runs(id),
 attempt_id TEXT REFERENCES attempts(id), operation TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX trace_links_run ON trace_links(run_id,created_at);
DELETE FROM schema_version;
INSERT INTO schema_version VALUES(3);
"""

VERSION_FOUR = r"""
ALTER TABLE trace_links ADD COLUMN request_id TEXT;
ALTER TABLE trace_links ADD COLUMN span_id TEXT;
ALTER TABLE trace_links ADD COLUMN parent_span_id TEXT;
ALTER TABLE trace_links ADD COLUMN traceparent TEXT;
ALTER TABLE trace_links ADD COLUMN intent_id TEXT REFERENCES publish_intents(id);
ALTER TABLE trace_links ADD COLUMN publication_attempt_id TEXT REFERENCES publish_attempts(id);
CREATE INDEX trace_links_publication ON trace_links(publication_attempt_id,created_at);
DELETE FROM schema_version;
INSERT INTO schema_version VALUES(4);
"""

VERSION_FIVE = r"""
CREATE TABLE studio_projects(id TEXT PRIMARY KEY,draft TEXT NOT NULL,version INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE studio_revisions(id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES studio_projects(id),number INTEGER NOT NULL,draft TEXT NOT NULL,input_digest TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(project_id,number));
CREATE TABLE studio_generations(id TEXT PRIMARY KEY,revision_id TEXT NOT NULL REFERENCES studio_revisions(id),run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),stage TEXT NOT NULL,input_digest TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE studio_requests(scope TEXT NOT NULL,key TEXT NOT NULL,input_digest TEXT NOT NULL,result TEXT NOT NULL,PRIMARY KEY(scope,key));
CREATE TABLE media_families(id TEXT PRIMARY KEY,parent_id TEXT REFERENCES media_families(id));
CREATE TABLE studio_assets(id TEXT PRIMARY KEY,name TEXT NOT NULL,kind TEXT NOT NULL,media_type TEXT NOT NULL,size INTEGER NOT NULL,sha256 TEXT NOT NULL,storage_ref TEXT NOT NULL UNIQUE,family_id TEXT NOT NULL REFERENCES media_families(id),parent_id TEXT REFERENCES studio_assets(id),origin TEXT NOT NULL,source TEXT NOT NULL,metadata TEXT NOT NULL,fingerprint TEXT,created_at TEXT NOT NULL);
CREATE INDEX studio_assets_sha ON studio_assets(sha256);
CREATE TABLE generation_assets(generation_id TEXT NOT NULL REFERENCES studio_generations(id),asset_id TEXT NOT NULL REFERENCES studio_assets(id),attempt_id TEXT REFERENCES attempts(id),PRIMARY KEY(generation_id,asset_id));
CREATE TABLE studio_accounts(id TEXT PRIMARY KEY,platform TEXT NOT NULL,stable_subject TEXT NOT NULL,label TEXT NOT NULL,tags TEXT NOT NULL,state TEXT NOT NULL,version INTEGER NOT NULL,credential_ref TEXT,checked_at TEXT,evidence_mode TEXT NOT NULL,UNIQUE(platform,stable_subject,evidence_mode));
CREATE TABLE auth_sessions(id TEXT PRIMARY KEY,owner_hash TEXT NOT NULL,platform TEXT NOT NULL,account_id TEXT REFERENCES studio_accounts(id),method TEXT NOT NULL,state TEXT NOT NULL,expires_at REAL NOT NULL,error_code TEXT,version INTEGER NOT NULL,token TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL);
CREATE TABLE release_batches(id TEXT PRIMARY KEY,asset_id TEXT NOT NULL REFERENCES studio_assets(id),frozen TEXT NOT NULL,input_digest TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE release_targets(id TEXT PRIMARY KEY,batch_id TEXT NOT NULL REFERENCES release_batches(id),account_id TEXT NOT NULL REFERENCES studio_accounts(id),platform TEXT NOT NULL,stable_subject TEXT NOT NULL,family_id TEXT NOT NULL REFERENCES media_families(id),frozen TEXT NOT NULL,state TEXT NOT NULL,version INTEGER NOT NULL,evidence_mode TEXT NOT NULL);
CREATE TABLE release_attempts(id TEXT PRIMARY KEY,target_id TEXT NOT NULL REFERENCES release_targets(id),token TEXT NOT NULL UNIQUE,state TEXT NOT NULL,lease_until REAL NOT NULL,started_at TEXT NOT NULL,ended_at TEXT);
CREATE TABLE release_receipts(id TEXT PRIMARY KEY,target_id TEXT NOT NULL REFERENCES release_targets(id),attempt_id TEXT REFERENCES release_attempts(id),created_at TEXT NOT NULL,source TEXT NOT NULL,outcome TEXT NOT NULL,work_id TEXT,evidence_code TEXT NOT NULL);
CREATE TABLE release_guards(platform TEXT NOT NULL,stable_subject TEXT NOT NULL,family_id TEXT NOT NULL REFERENCES media_families(id),evidence_mode TEXT NOT NULL,target_id TEXT NOT NULL REFERENCES release_targets(id),PRIMARY KEY(platform,stable_subject,family_id,evidence_mode));
CREATE TABLE release_account_locks(account_id TEXT PRIMARY KEY REFERENCES studio_accounts(id),token TEXT NOT NULL UNIQUE);
CREATE TABLE studio_jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,subject_id TEXT NOT NULL,state TEXT NOT NULL,token TEXT NOT NULL UNIQUE,deadline REAL NOT NULL,context TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX studio_jobs_queue ON studio_jobs(state,created_at);
DELETE FROM schema_version;
INSERT INTO schema_version VALUES(5);
"""

CURRENT_VERSION = 5

def statements(db, script):
    for statement in script.split(';'):
        if statement.strip():
            db.execute(statement)

def migrate(db):
    # Foreign keys are disabled only for the transactional parent-table replacement.
    # Check them before commit and restore enforcement even on failure.
    db.execute('PRAGMA foreign_keys=OFF')
    try:
        db.execute('BEGIN IMMEDIATE')
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='schema_version'").fetchone()
        if exists:
            versions = [r[0] for r in db.execute('SELECT version FROM schema_version')]
            if versions not in ([1], [2], [3], [4], [5]):
                raise RuntimeError('Unsupported state database version')
            version = versions[0]
        else:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                raise RuntimeError('Unversioned state database')
            statements(db, VERSION_ONE)
            version = 1
        if version == 1:
            statements(db, VERSION_TWO)
            version = 2
        if version == 2:
            statements(db, VERSION_THREE)
            version = 3
        if version == 3:
            statements(db, VERSION_FOUR)
            version = 4
        if version == 4:
            statements(db, VERSION_FIVE)
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('Invalid state database references')
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.execute('PRAGMA foreign_keys=ON')
