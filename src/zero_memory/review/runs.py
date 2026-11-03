"""从候选的审核备注中解析提炼批次编号，不改记忆 schema。"""

import re

RUN_NOTE = re.compile(r"^DeepSeek (批次|试点) ([0-9a-f]{64})")
KINDS = {"批次": "batch", "试点": "pilot"}


def run_of(note: str | None) -> dict | None:
    match = RUN_NOTE.match(note or "")
    return {"run_id": match.group(2), "kind": KINDS[match.group(1)]} if match else None


def run_like(run_id: str) -> str:
    # 「批次」「试点」都是两个字，用 LIKE 的单字符通配同时匹配
    return f"DeepSeek __ {run_id}%"
