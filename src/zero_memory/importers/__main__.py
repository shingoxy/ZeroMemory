"""Import raw history without starting a server or calling a model."""

import argparse
import sys
from pathlib import Path

from zero_memory.core.config import Settings
from zero_memory.core.errors import ServiceError
from zero_memory.core.store import Store, dumps
from zero_memory.importers.service import Importer
from zero_memory.schemas.models import Scope


def main():
    parser = argparse.ArgumentParser(description="本地导入 ZIP/JSON/JSONL 原始聊天，不自动调用模型")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--home", type=Path, default=Path("data"))
    parser.add_argument("--user-id", default="default-user")
    parser.add_argument("--project-id", default="default")
    args = parser.parse_args()
    try:
        settings = Settings(_env_file=None, home=args.home, llm_provider="none", allow_cloud=False)
        importer = Importer(Store(settings.home), settings)
        scope = Scope(user_id=args.user_id, project_id=args.project_id)
        with args.input.open("rb") as source:
            job = importer.preview(source, args.input.name, scope)
        importer.start(job["id"], scope)
        importer.run(job["id"], scope)
        job = importer.get(job["id"], scope)
        print(
            dumps(
                {
                    k: job[k]
                    for k in (
                        "id",
                        "status",
                        "source_hash",
                        "total_messages",
                        "inserted",
                        "duplicates",
                        "failed",
                        "waiting_extraction",
                        "errors",
                    )
                }
                | {"conversations": len(job["conversations"])}
            )
        )
        return int(job["status"] != "completed")
    except (ServiceError, ValueError, OSError):
        print("导入失败：请检查输入格式、文件大小和本地数据目录权限。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
