"""在现有启动排他锁及事务内执行有版本记录的增量迁移。"""
from psycopg import Cursor


def migrate(cur: Cursor) -> None:
    cur.execute("CREATE TABLE IF NOT EXISTS app_migrations (version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    cur.execute("SELECT 1 FROM app_migrations WHERE version = 1")
    if cur.fetchone() is not None:
        return
    cur.execute("""
        ALTER TABLE generations ADD COLUMN result_kind TEXT
          CHECK (result_kind IN ('answered','clarification','no_evidence','answer_rejected','dependency_failed'));
        ALTER TABLE generations ADD COLUMN error_code TEXT;
        ALTER TABLE generations ADD COLUMN trace_id VARCHAR(32);
        ALTER TABLE generations ADD COLUMN attempt_id VARCHAR(32);
        ALTER TABLE generations ADD COLUMN retry_of BIGINT REFERENCES generations(id) ON DELETE SET NULL;
        ALTER TABLE generations ADD CONSTRAINT generation_result_status CHECK (
          result_kind IS NULL OR
          (status='completed' AND result_kind IN ('answered','clarification','no_evidence')) OR
          (status='failed' AND result_kind IN ('answer_rejected','dependency_failed'))
        );
        CREATE INDEX idx_generations_stale ON generations(heartbeat_at) WHERE status = 'running';
        CREATE TABLE generation_attempts (
          id VARCHAR(32) PRIMARY KEY,
          generation_id BIGINT NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
          trace_id VARCHAR(32) NOT NULL UNIQUE,
          worker_id TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('running','completed','failed','cancelled')),
          error_code TEXT,
          started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          finished_at TIMESTAMPTZ
        );
        CREATE TABLE trace_spans (
          span_id VARCHAR(16) PRIMARY KEY, trace_id VARCHAR(32) NOT NULL,
          parent_span_id VARCHAR(16), user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          project_id VARCHAR(36), generation_id BIGINT REFERENCES generations(id) ON DELETE CASCADE,
          attempt_id VARCHAR(32), name TEXT NOT NULL,
          started_at TIMESTAMPTZ NOT NULL, duration_ms DOUBLE PRECISION NOT NULL CHECK (duration_ms >= 0),
          status TEXT NOT NULL CHECK (status IN ('ok','error','cancelled')),
          attributes JSONB NOT NULL CHECK (jsonb_typeof(attributes) = 'object')
        );
        CREATE INDEX idx_trace_spans_owner ON trace_spans(user_id, trace_id, started_at);
        CREATE INDEX idx_trace_spans_retention ON trace_spans(started_at);
        CREATE TABLE audit_events (
          id BIGSERIAL PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
          operation_id TEXT NOT NULL, user_id INTEGER,
          actor_type TEXT NOT NULL CHECK (actor_type IN ('user','system','anonymous')),
          action TEXT NOT NULL, resource_type TEXT NOT NULL, resource_id TEXT NOT NULL,
          project_id VARCHAR(36), result TEXT NOT NULL
            CHECK (result IN ('started','accepted','succeeded','failed','denied','partial','cancelled')),
          error_code TEXT, http_request_id TEXT, trace_id VARCHAR(32),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          details JSONB NOT NULL CHECK (jsonb_typeof(details) = 'object')
        );
        CREATE INDEX idx_audit_events_owner ON audit_events(user_id, id DESC);
        CREATE INDEX idx_audit_events_operation ON audit_events(operation_id, id);
        CREATE INDEX idx_audit_events_retention ON audit_events(created_at, id);
        CREATE FUNCTION protect_audit_events() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' AND OLD.created_at < now() - interval '90 days' THEN
            RETURN OLD;
          END IF;
          RAISE EXCEPTION '审计事件只允许追加';
        END $$;
        CREATE TRIGGER audit_events_immutable BEFORE UPDATE OR DELETE ON audit_events
          FOR EACH ROW EXECUTE FUNCTION protect_audit_events();
        ALTER TABLE upload_task_items ADD COLUMN recovery JSONB CHECK (recovery IS NULL OR (
          jsonb_typeof(recovery)='object' AND recovery ? 'stage' AND
          recovery->>'stage' IS NOT NULL AND recovery->>'stage' IN (
            'prepared','replacing','indexing','writing','publishing','published','cleaned','rolled_back','unpublished'
          )
        ));
        ALTER TABLE upload_tasks ADD COLUMN trace_id VARCHAR(32);
        INSERT INTO app_migrations(version) VALUES (1);
    """)
