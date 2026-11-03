import json
import shutil
import zipfile
from uuid import uuid4

from zero_memory.activity.migrations import EXTENSION_TABLES, migrate
from zero_memory.core.errors import ServiceError
from zero_memory.core.identity import Identity
from zero_memory.core.store import Store, digest, dumps
from zero_memory.importers.chatgpt import safe_zip
from zero_memory.importers.raw_history import RAW_TABLES, rebuild_index
from zero_memory.importers.raw_history import install as install_raw_history
from zero_memory.importers.service import Importer
from zero_memory.memory.sqlite import SQLiteBackend
from zero_memory.schemas.models import Memory, Soul, Style

TABLES = {
    "memories": ("id", "user_id", "agent_id", "project_id", "status", "content", "body"),
    "revisions": ("memory_id", "version", "body"),
    "messages": (
        "key",
        "user_id",
        "agent_id",
        "project_id",
        "conversation_id",
        "message_id",
        "content_hash",
        "role",
        "timestamp",
        "content",
        "body",
    ),
    "conversations": ("user_id", "agent_id", "project_id", "id", "body"),
    "imports": ("id", "body"),
    "extraction_tasks": ("message_key", "status", "error"),
    "audit": ("id", "time", "action", "actor", "target", "details"),
    "chat_messages": (
        "id",
        "user_id",
        "agent_id",
        "project_id",
        "session_id",
        "role",
        "content",
        "timestamp",
    ),
}


class Backups:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings

    def create(self):
        backup_id = str(uuid4())
        path = self.store.home / "backups" / (backup_id + ".zip")
        manifest = {"format": "zero-memory", "version": 1, "files": {}}
        # Hold the common writer lock for a coherent identity + database + source snapshot.
        with (
            self.store.lock(),
            self.store.read() as db,
            zipfile.ZipFile(path, "x", compression=zipfile.ZIP_STORED) as archive,
        ):

            def put(name, data):
                archive.writestr(name, data)
                manifest["files"][name] = digest(data)

            for folder in ("identity", "sources", "activity_artifacts"):
                for source in sorted((self.store.home / folder).rglob("*")):
                    if (
                        source.is_file()
                        and not source.is_symlink()
                        and source.suffix not in {".lock", ".upload", ".tmp"}
                    ):
                        data = source.read_bytes()
                        put(source.relative_to(self.store.home).as_posix(), data)
            present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in {**TABLES, **EXTENSION_TABLES, **RAW_TABLES}:
                if table not in present:
                    continue
                lines = "".join(dumps(dict(r)) + "\n" for r in db.execute(f"SELECT * FROM {table}"))
                put(f"tables/{table}.jsonl", lines.encode("utf-8"))
            for row in db.execute("SELECT id,body FROM memories"):
                memory = Memory.model_validate_json(row["body"])
                put(
                    f"readable/{memory.memory_id}.md",
                    (
                        "# "
                        + memory.memory_type
                        + "\n\n"
                        + memory.content
                        + "\n\n## Provenance\n```json\n"
                        + memory.model_dump_json(indent=2)
                        + "\n```\n"
                    ).encode("utf-8"),
                )
            archive.writestr("manifest.json", dumps(manifest))
        return {
            "backup_id": backup_id,
            "filename": path.name,
            "size": path.stat().st_size,
            "download_url": f"/v1/backups/{backup_id}",
            "contains_personal_data": True,
            "note": "本地未加密备份，包含原始来源与修订；不含令牌或模型密钥",
        }

    def restore(self, stream):
        restore_id = str(uuid4())
        upload = self.store.home / "backups" / (restore_id + ".restore-upload")
        staging = self.store.home / "backups" / (restore_id + ".staging")
        target = self.store.home / "backups" / ("restored-" + restore_id)
        try:
            total = 0
            with upload.open("xb") as output:
                while chunk := stream.read(1024 * 1024):
                    total += len(chunk)
                    if total > self.settings.max_expanded_bytes:
                        raise ServiceError("backup_limit", "备份超过恢复大小限制", 413)
                    output.write(chunk)
            with safe_zip(upload, self.settings, max_files=50000) as archive:
                try:
                    manifest = json.loads(archive.read("manifest.json"))
                    if manifest["format"] != "zero-memory" or manifest["version"] != 1:
                        raise ValueError()
                    files = manifest["files"]
                    if not isinstance(files, dict) or set(archive.namelist()) != set(files) | {
                        "manifest.json"
                    }:
                        raise ValueError()
                    for name, checksum in files.items():
                        if digest(archive.read(name)) != checksum:
                            raise ValueError()
                    for table in TABLES:
                        if f"tables/{table}.jsonl" not in files:
                            raise ValueError()
                    current = json.loads(archive.read("identity/current.json"))
                    snapshot = json.loads(archive.read(f"identity/versions/{current['version']:06}.json"))
                    Soul.model_validate(snapshot["soul"])
                    Style.model_validate(snapshot["style"])
                except (KeyError, ValueError, TypeError):
                    raise ServiceError("backup_integrity", "备份格式、清单或校验值不匹配", 422) from None
                restored = Store(staging)
                Importer(restored, self.settings)
                extensions = [t for t in EXTENSION_TABLES if f"tables/{t}.jsonl" in files]
                if extensions and set(extensions) != set(EXTENSION_TABLES):
                    raise ServiceError("backup_schema", "扩展表清单不完整", 422)
                if extensions:
                    migrate(restored)
                raw_tables = {t: columns for t, columns in RAW_TABLES.items() if f"tables/{t}.jsonl" in files}
                if raw_tables and raw_tables != RAW_TABLES:
                    raise ServiceError("backup_schema", "原始历史表清单不完整", 422)
                with restored.write() as db:
                    if raw_tables:
                        install_raw_history(db)
                    for table, columns in {**TABLES, **{t: EXTENSION_TABLES[t] for t in extensions}, **raw_tables}.items():
                        db.execute(f"DELETE FROM {table}")
                        with archive.open(f"tables/{table}.jsonl") as source:
                            for line in source:
                                row = json.loads(line)
                                if set(row) != set(columns):
                                    raise ServiceError("backup_schema", "备份表结构不兼容", 422)
                                if table == "memories":
                                    memory = Memory.model_validate_json(row["body"])
                                    if [
                                        memory.memory_id,
                                        memory.user_id,
                                        memory.agent_id,
                                        memory.project_id,
                                        memory.status,
                                        memory.content,
                                    ] != [row[k] for k in columns[:-1]]:
                                        raise ServiceError("backup_schema", "记忆元数据不一致", 422)
                                db.execute(
                                    f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                                    [row[k] for k in columns],
                                )
                    if raw_tables:
                        rebuild_index(db)
                for name in files:
                    if name.startswith(("identity/", "sources/", "activity_artifacts/")):
                        destination = staging / name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(archive.read(name))
                Identity(restored)
                SQLiteBackend(restored).rebuild()
                Importer(restored, self.settings).recover()
                staging.rename(target)
            return {
                "restored_home": str(target),
                "activated": False,
                "note": "已恢复到全新目录，当前数据未覆盖。停止服务后设置 ZERO_HOME 为此目录并重启；客户端令牌需重新创建。",
            }
        except ServiceError:
            raise
        except Exception:
            raise ServiceError(
                "backup_restore", "恢复失败：格式损坏或数据不兼容，当前数据保持不变", 422
            ) from None
        finally:
            upload.unlink(missing_ok=True)
            if staging.exists():
                # staging is a fixed child of the configured backup directory, never a user-provided path.
                assert staging.resolve().parent == (self.store.home / "backups").resolve()
                shutil.rmtree(staging)
