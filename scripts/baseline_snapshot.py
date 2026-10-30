"""Local preservation fingerprints; never export memory text or credentials."""

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

from zero_memory.core.store import Store, atomic_json, digest, dumps

LEGACY_TABLES = (
    "memories",
    "revisions",
    "messages",
    "conversations",
    "imports",
    "extraction_tasks",
    "chat_messages",
)


def fingerprint(store, audit_count=None):
    with store.lock(), store.read() as db:
        tables = {}
        for table in LEGACY_TABLES:
            rows = sorted(dumps(dict(r)) for r in db.execute(f"SELECT * FROM {table}"))
            tables[table] = {"count": len(rows), "sha256": digest(dumps(rows))}
        rows = db.execute("SELECT id,body FROM memories ORDER BY id").fetchall()
        memories = [json.loads(r["body"]) for r in rows]
        result = {
            "tables": tables,
            "memory_ids_sha256": digest(dumps([r["id"] for r in rows])),
            "statuses": dict(Counter(m["status"] for m in memories)),
            "types": dict(Counter(m["memory_type"] for m in memories)),
            "scope_count": len({(m["user_id"], m["agent_id"], m["project_id"]) for m in memories}),
            "source_types": dict(Counter(m["source_type"] for m in memories)),
            "provenance_sha256": digest(
                dumps(
                    [
                        {
                            k: m.get(k)
                            for k in (
                                "memory_id",
                                "source_type",
                                "source_conversation_id",
                                "source_message_ids",
                                "source_message_keys",
                                "source_artifact",
                                "confidence",
                                "status",
                                "valid_from",
                                "valid_to",
                                "version",
                            )
                        }
                        for m in memories
                    ]
                )
            ),
        }
        count = (
            audit_count if audit_count is not None else db.execute("SELECT count(*) FROM audit").fetchone()[0]
        )
        audit = [dict(r) for r in db.execute("SELECT * FROM audit ORDER BY id LIMIT ?", (count,))]
        result["audit_prefix"] = {"count": count, "sha256": digest(dumps(audit))}
        for folder in ("identity", "sources"):
            result[folder] = {
                p.relative_to(store.home).as_posix(): digest(p.read_bytes())
                for p in sorted((store.home / folder).rglob("*"))
                if p.is_file() and p.suffix not in {".lock", ".tmp", ".upload"}
            }
        return result


def preflight(home):
    path = home / "memory" / "zero.sqlite3"
    if not path.exists():
        raise ValueError("Existing database required; refusing to initialize production")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Database integrity check failed")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not set(LEGACY_TABLES).issubset(tables):
            raise ValueError("Unknown database layout; no writes permitted")
        if db.execute("PRAGMA user_version").fetchone()[0] not in {0, 2}:
            raise ValueError("Unknown database version; no writes permitted")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()
    home = args.home.resolve()
    preflight(home)
    baseline = json.loads(args.compare.read_text(encoding="utf-8")) if args.compare else None
    value = fingerprint(Store(home), baseline["audit_prefix"]["count"] if baseline else None)
    if baseline and value != baseline:
        raise SystemExit("Preservation check FAILED; inspect private snapshots")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(args.output, value)
    print(
        dumps(
            {k: value[k] for k in ("statuses", "types", "scope_count", "source_types", "memory_ids_sha256")}
        )
    )
