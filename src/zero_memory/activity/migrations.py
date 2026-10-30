"""Additive v0.2 extension. Legacy tables and identity files are never rewritten."""

from zero_memory.core.errors import ServiceError
from zero_memory.core.store import digest, dumps

ENTITY_TABLES = (
    "goals",
    "episodes",
    "activity_sources",
    "activity_artifacts",
    "agent_interests",
    "research_methods",
    "method_attempts",
    "persona_proposals",
)
EXTENSION_TABLES = {name: ("id", "user_id", "agent_id", "project_id", "body") for name in ENTITY_TABLES}
EXTENSION_TABLES.update(
    {
        "v02_migrations": ("name", "version", "definition_hash"),
        "goal_events": ("id", "user_id", "agent_id", "project_id", "goal_id", "body"),
        "activity_runs": ("id", "user_id", "agent_id", "project_id", "goal_id", "request_id", "body"),
        "method_revisions": ("id", "version", "body"),
        "persona_states": ("user_id", "agent_id", "project_id", "body"),
        "persona_events": ("id", "user_id", "agent_id", "project_id", "body"),
    }
)
DEFINITIONS = [
    *(
        f"CREATE TABLE IF NOT EXISTS {name}(id TEXT PRIMARY KEY,user_id TEXT,agent_id TEXT,project_id TEXT,body TEXT NOT NULL)"
        for name in ENTITY_TABLES
    ),
    "CREATE TABLE IF NOT EXISTS v02_migrations(name TEXT PRIMARY KEY,version INTEGER,definition_hash TEXT)",
    "CREATE TABLE IF NOT EXISTS goal_events(id TEXT PRIMARY KEY,user_id TEXT,agent_id TEXT,project_id TEXT,goal_id TEXT,body TEXT)",
    "CREATE TABLE IF NOT EXISTS activity_runs(id TEXT PRIMARY KEY,user_id TEXT,agent_id TEXT,project_id TEXT,goal_id TEXT,request_id TEXT,body TEXT,UNIQUE(user_id,agent_id,project_id,request_id))",
    "CREATE TABLE IF NOT EXISTS method_revisions(id TEXT,version INTEGER,body TEXT,PRIMARY KEY(id,version))",
    "CREATE TABLE IF NOT EXISTS persona_states(user_id TEXT,agent_id TEXT,project_id TEXT,body TEXT,PRIMARY KEY(user_id,agent_id,project_id))",
    "CREATE TABLE IF NOT EXISTS persona_events(id TEXT PRIMARY KEY,user_id TEXT,agent_id TEXT,project_id TEXT,body TEXT)",
    "CREATE UNIQUE INDEX IF NOT EXISTS episodes_source ON episodes(user_id,agent_id,project_id,json_extract(body,'$.source_type'),json_extract(body,'$.source_id'))",
    "CREATE UNIQUE INDEX IF NOT EXISTS interest_topic ON agent_interests(user_id,agent_id,project_id,json_extract(body,'$.topic'))",
    "CREATE UNIQUE INDEX IF NOT EXISTS attempt_goal ON method_attempts(user_id,agent_id,project_id,json_extract(body,'$.goal_id'))",
]
DEFINITION_HASH = digest(dumps(DEFINITIONS))


def migrate(store):
    with store.write() as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 2}:
            raise ServiceError("schema_version", "未知数据库版本，拒绝迁移", 503)
        marker = db.execute("SELECT 1 FROM sqlite_master WHERE name='v02_migrations'").fetchone()
        if marker:
            row = db.execute(
                "SELECT version,definition_hash FROM v02_migrations WHERE name='activity'"
            ).fetchone()
            if not row or tuple(row) != (1, DEFINITION_HASH):
                raise ServiceError("schema_version", "未知扩展迁移版本，拒绝修改", 503)
        elif version == 2:
            raise ServiceError("schema_version", "扩展迁移标记缺失，拒绝修改", 503)
        for statement in DEFINITIONS:
            db.execute(statement)
        for name, columns in EXTENSION_TABLES.items():
            if tuple(r[1] for r in db.execute(f"PRAGMA table_info({name})")) != columns:
                raise ServiceError("schema_layout", "扩展表结构不匹配，已回滚", 503)
        if not marker:
            db.execute("INSERT INTO v02_migrations VALUES('activity',1,?)", (DEFINITION_HASH,))
            store.audit(db, "schema_migration", "service", "activity:v1", {"additive": True})
        db.execute("PRAGMA user_version=2")
