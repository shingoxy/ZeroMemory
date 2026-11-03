"""审核模块的扩展表：只新增，不改动原有表结构；首次写入时才创建。"""

from zero_memory.core.errors import ServiceError

COLUMNS = ("id", "user_id", "agent_id", "project_id", "body")
REVIEW_TABLES = {"review_marks": COLUMNS, "memory_links": COLUMNS}
DDL = [
    *(f"CREATE TABLE IF NOT EXISTS {name}(id TEXT PRIMARY KEY, user_id TEXT NOT NULL, agent_id TEXT NOT NULL, "
      "project_id TEXT NOT NULL, body TEXT NOT NULL)" for name in REVIEW_TABLES),
    "CREATE INDEX IF NOT EXISTS memory_links_scope ON memory_links(user_id, agent_id, project_id)",
]


def initialized(db) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='review_marks'").fetchone() is not None


def install(db) -> None:
    if db.execute("PRAGMA user_version").fetchone()[0] not in (0, 2):
        raise ServiceError("schema_version", "未知数据库版本")
    for statement in DDL:
        db.execute(statement)
    for table, columns in REVIEW_TABLES.items():
        if tuple(row[1] for row in db.execute(f"PRAGMA table_info({table})")) != columns:
            raise ServiceError("review_schema", "审核表结构不兼容")
