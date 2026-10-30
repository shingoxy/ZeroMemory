import argparse
import os
import sqlite3
import sys

import uvicorn
from pydantic import ValidationError

from zero_memory.core.config import Settings
from zero_memory.core.errors import ServiceError


def main():
    parser = argparse.ArgumentParser(description="零的记忆：本地身份与长期记忆服务")
    parser.add_argument("--home", help="数据目录（默认 ZERO_HOME 或 ./data）")
    parser.add_argument("--port", type=int, help="本机监听端口")
    args = parser.parse_args()
    if args.home:
        os.environ["ZERO_HOME"] = args.home
    if args.port:
        os.environ["ZERO_PORT"] = str(args.port)
    try:
        settings = Settings()
        from zero_memory.api.app import create_app

        app = create_app(settings)
        print(f"ZeroMemory: http://{settings.host}:{settings.port} | data: {settings.home}")
        uvicorn.run(app, host=settings.host, port=settings.port, access_log=False)
    except (ValidationError, ServiceError, OSError, sqlite3.Error, ValueError) as exc:
        message = (
            exc.message
            if isinstance(exc, ServiceError)
            else "请检查 ZERO_* 配置、数据目录权限、端口及磁盘空间"
        )
        print("ZeroMemory 启动失败：" + message, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
