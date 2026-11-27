"""索引归属迁移及跨磁盘、向量库删除操作的持久化记录。"""
import os
import uuid
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from utils.core.project_paths import scoped_knowledge_file
from utils.indexing.upload_quota import moving_upload_files
from utils.observability.audit import append_audit, resource_digest


def migrate_deletions(cur):
    cur.execute("SELECT 1 FROM app_migrations WHERE version = 2")
    if cur.fetchone():
        return
    cur.execute("""
        ALTER TABLE users ADD COLUMN deletion_pending BOOLEAN NOT NULL DEFAULT FALSE;
        ALTER TABLE projects ADD COLUMN deletion_pending BOOLEAN NOT NULL DEFAULT FALSE
            CHECK (NOT deletion_pending OR deleted_at IS NOT NULL);
        ALTER TABLE projects ADD COLUMN deletion_disk_stage TEXT NOT NULL DEFAULT 'pending'
            CHECK (deletion_disk_stage IN ('pending','quarantined','removed'));
        CREATE INDEX idx_users_deleting ON users(id) WHERE deletion_pending;
        CREATE INDEX idx_projects_deleting ON projects(user_id,id) WHERE deletion_pending;
        CREATE TABLE pending_file_deletions (
            user_id INTEGER NOT NULL, project_id VARCHAR(36) NOT NULL, filename TEXT NOT NULL,
            id UUID NOT NULL UNIQUE, stage TEXT NOT NULL DEFAULT 'pending'
                CHECK (stage IN ('pending','quarantined','index_removed')),
            namespace TEXT NOT NULL,
            PRIMARY KEY (user_id, project_id, filename),
            FOREIGN KEY (user_id, project_id) REFERENCES projects(user_id, id) ON DELETE CASCADE
        );
        CREATE TABLE index_namespaces (
            namespace TEXT PRIMARY KEY, user_id INTEGER NOT NULL, project_id VARCHAR(36) NOT NULL,
            milvus_uri TEXT, collection_name TEXT,
            CHECK ((milvus_uri IS NULL) = (collection_name IS NULL)),
            FOREIGN KEY (user_id, project_id) REFERENCES projects(user_id, id) ON DELETE CASCADE
        );
        CREATE INDEX idx_index_namespaces_project ON index_namespaces(user_id, project_id);
        ALTER TABLE pending_file_deletions ADD FOREIGN KEY (namespace) REFERENCES index_namespaces(namespace) ON DELETE CASCADE;
        CREATE TEMP TABLE legacy_index_owners ON COMMIT DROP AS
            WITH sources AS (
                SELECT namespace, source FROM index_manifests UNION ALL
                SELECT namespace, source FROM indexed_source_files UNION ALL
                SELECT namespace, source FROM published_source_files
            ), paths AS (
                SELECT namespace, regexp_match(replace(source, chr(92), '/'),
                    '/([1-9][0-9]*)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/[^/]+$') AS owner
                FROM sources
            )
            SELECT namespace, min(owner[1]) AS uid, min(owner[2]) AS pid,
                   bool_and(owner IS NOT NULL) AND count(DISTINCT owner) = 1 AS verified
            FROM paths GROUP BY namespace;
    """)
    cur.execute("""SELECT namespace FROM legacy_index_owners o LEFT JOIN projects p ON p.id=o.pid
                   WHERE NOT verified OR (p.id IS NOT NULL AND p.user_id::text <> o.uid) LIMIT 1""")
    invalid = cur.fetchone()
    if invalid:
        raise RuntimeError(f"旧索引的项目归属不明确，请核对命名空间 {invalid[0]} 的来源记录后重新启动")
    cur.execute("""
        INSERT INTO index_namespaces(namespace, user_id, project_id)
            SELECT o.namespace, p.user_id, p.id FROM legacy_index_owners o
            JOIN projects p ON p.id=o.pid AND p.user_id::text=o.uid;
        -- 归属已核实且项目已被硬删除的旧数据，包括经外键关联的原文快照。
        DELETE FROM indexed_source_files f USING legacy_index_owners o
            WHERE f.namespace=o.namespace AND NOT EXISTS (SELECT 1 FROM projects WHERE id=o.pid);
        DELETE FROM index_manifests m USING legacy_index_owners o
            WHERE m.namespace=o.namespace AND NOT EXISTS (SELECT 1 FROM projects WHERE id=o.pid);
        ALTER TABLE index_manifests ADD FOREIGN KEY (namespace) REFERENCES index_namespaces(namespace) ON DELETE CASCADE;
        ALTER TABLE indexed_source_files ADD FOREIGN KEY (namespace) REFERENCES index_namespaces(namespace) ON DELETE CASCADE;
        UPDATE projects p SET deletion_pending=TRUE, deletion_disk_stage='removed' WHERE deleted_at IS NOT NULL
            AND EXISTS (SELECT 1 FROM index_namespaces n WHERE n.project_id=p.id);
        INSERT INTO app_migrations(version) VALUES (2);
    """)
    # 在配置下次变更之前，为当前仍可识别的历史命名空间保存实际位置。
    from rag.index_manifest import ManifestStore
    from utils.core.config_loader import vector_db_config
    cur.execute("SELECT DISTINCT user_id, project_id FROM index_namespaces")
    for user_id, project_id in cur.fetchall():
        store = ManifestStore(vector_db_config, user_id, project_id)
        cur.execute("UPDATE index_namespaces SET milvus_uri=%s, collection_name=%s WHERE namespace=%s",
                    (store.uri, store.collection_name, store.namespace))


def pending_deletions(conninfo: str):
    """父资源接管子资源的清理。所有执行者取锁后会再次核对状态。"""
    with psycopg.connect(conninfo) as conn:
        users = conn.execute("SELECT id FROM users WHERE deletion_pending ORDER BY id").fetchall()
        projects = conn.execute("""SELECT p.user_id,p.id FROM projects p JOIN users u ON u.id=p.user_id
                                   WHERE p.deletion_pending AND NOT u.deletion_pending ORDER BY p.id""").fetchall()
        files = conn.execute("""SELECT DISTINCT d.user_id,d.project_id FROM pending_file_deletions d
                                JOIN projects p ON p.id=d.project_id WHERE p.deleted_at IS NULL""").fetchall()
    return [row[0] for row in users], projects, files


def file_deletions(conninfo, user_id, project_id):
    with psycopg.connect(conninfo, row_factory=dict_row) as conn:
        return conn.execute("""SELECT d.id, d.filename, d.stage, n.milvus_uri, n.collection_name
                               FROM pending_file_deletions d JOIN index_namespaces n USING(namespace,user_id,project_id)
                               WHERE d.user_id=%s AND d.project_id=%s ORDER BY d.filename""", (user_id, project_id)).fetchall()


def begin_file_deletion(manifests, filename):
    with psycopg.connect(manifests.conninfo) as conn, conn.cursor() as cur:
        manifests.register_location(conn)
        cur.execute("INSERT INTO pending_file_deletions(user_id, project_id, filename, id, namespace) VALUES (%s,%s,%s,%s,%s)",
                    (manifests.user_id, manifests.project_id, filename, uuid.uuid4(), manifests.namespace))
        append_audit(cur, user_id=manifests.user_id, project_id=manifests.project_id, action="file.delete", resource_type="file",
                     resource_id=resource_digest(filename), result="accepted")


def finish_file_deletion(conninfo, user_id, project_id, directory, job, delete_index):
    """调用方持项目写锁。暂存名只属于这次删除，接管前不会有同名覆盖上传。"""
    source = Path(scoped_knowledge_file(directory, job["filename"]))
    staged = Path(scoped_knowledge_file(directory, f".deleting-{job['id']}"))
    Path(directory).stat()
    stage = job["stage"]
    if stage == "pending":
        try:
            staged.stat()
        except FileNotFoundError:
            with moving_upload_files(conninfo, user_id):
                os.rename(source, staged)
        else:
            if source.exists():
                raise RuntimeError("原文件和删除暂存文件同时存在，请核对存储")
        with psycopg.connect(conninfo) as conn:
            conn.execute("UPDATE pending_file_deletions SET stage='quarantined' WHERE id=%s", (job["id"],))
        stage = "quarantined"
    if source.exists():
        raise RuntimeError("待删除路径出现新文件，请核对存储")
    if stage == "quarantined":
        staged.stat()  # 暂存凭据也消失时应等待原存储恢复。
        delete_index(str(source))
        with psycopg.connect(conninfo) as conn:
            conn.execute("UPDATE pending_file_deletions SET stage='index_removed' WHERE id=%s", (job["id"],))
    staged.unlink(missing_ok=True)
    with psycopg.connect(conninfo) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM pending_file_deletions WHERE id=%s", (job["id"],))
        append_audit(cur, user_id=user_id, project_id=project_id, action="file.delete", resource_type="file",
                     resource_id=resource_digest(job["filename"]), result="succeeded")
