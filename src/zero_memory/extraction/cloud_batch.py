"""Execute one locally reviewed DeepSeek batch with a resumable journal."""

import argparse
import json
import re
import sqlite3
import unicodedata
from collections import Counter
from contextlib import closing
from pathlib import Path

import httpx
import portalocker

from zero_memory.core.store import Store, atomic_json, digest, dumps, now
from zero_memory.extraction.cloud_preview import MAX_EXPANDED_SAMPLES, SENSITIVE, _rows, reopen
from zero_memory.extraction.service import Candidate
from zero_memory.llm.windows_credentials import read_deepseek_key
from zero_memory.memory.sqlite import SQLiteBackend
from zero_memory.schemas.models import Proposal, Scope

ALLOWED_TYPES = {"user_profile", "episodic", "semantic", "procedural", "working_context"}


def save_candidate(home, manifest, scope, body, entry, response):
    value = json.loads(response)
    if not isinstance(value, dict) or set(value) != {'memories'} or not isinstance(value['memories'], list):
        return {'status': 'invalid_output'}
    items = value['memories']
    if not items:
        return {'status': 'no_candidate'}
    if len(items) != 1 or not isinstance(items[0], dict):
        return {'status': 'invalid_output'}
    item = dict(items[0])
    event_time = item.pop('event_time', None)
    candidate = Candidate.model_validate(item)
    source = json.loads(body['messages'][1]['content'])['untrusted_user_text']
    if not candidate.evidence.strip() or candidate.evidence not in source or not candidate.content.strip():
        return {'status': 'evidence_rejected'}
    if candidate.memory_type not in ALLOWED_TYPES or candidate.summary_links:
        return {'status': 'invalid_output'}
    with closing(_rows(home)) as db:
        row = db.execute('SELECT conversation_id,message_id,role,timestamp,content FROM messages WHERE key=? '
                         'AND user_id=? AND agent_id=? AND project_id=?', (entry['message_key'], *scope.sql())).fetchone()
    if not row or row['role'] != 'user' or candidate.evidence not in row['content']:
        return {'status': 'evidence_rejected'}
    normalized = ' '.join(unicodedata.normalize('NFKC', candidate.content).split()).casefold()
    dedup_key = digest(dumps([entry['message_key'], normalized, manifest['prompt_version']]))
    date = event_time if isinstance(event_time, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', event_time) and event_time in candidate.evidence else None
    sensitivity = 'sensitive' if candidate.sensitivity == 'sensitive' or SENSITIVE.search(candidate.content) else 'private'
    if sensitivity == 'sensitive':
        folder = home / 'review_queue/v03b/sensitive'
        folder.mkdir(parents=True, exist_ok=True)
        atomic_json(folder / (dedup_key + '.json'), {'status': 'pending_review', 'run_id': manifest['preview_id'],
                    'source_message_key': entry['message_key'], 'source_message_id': row['message_id'],
                    'source_conversation_id': row['conversation_id'], 'observed_at': row['timestamp'],
                    'event_date': date, 'candidate': candidate.model_dump(), 'sensitivity': 'sensitive'})
        return {'status': 'sensitive_pending'}
    proposal = Proposal(content=candidate.content, evidence=candidate.evidence, memory_type=candidate.memory_type,
                        confidence=candidate.confidence, sensitivity='private', claim_kind=candidate.claim_kind,
                        source_type='chatgpt_export', source_conversation_id=row['conversation_id'],
                        source_message_ids=[row['message_id']], source_message_keys=[entry['message_key']],
                        origin_quality='verbatim_export_text', inference_type='system_summary',
                        event_time_original=date, event_time_precision='day' if date else None, dedup_key=dedup_key,
                        review_note=f'DeepSeek 批次 {manifest["preview_id"]} / {manifest["prompt_version"]} / {manifest["model"]}。来源时间 {row["timestamp"] or "未知"}。')
    with closing(_rows(home)) as db:
        exists = db.execute("SELECT id FROM memories WHERE user_id=? AND agent_id=? AND project_id=? "
                            "AND json_extract(body,'$.dedup_key')=?", (*scope.sql(), dedup_key)).fetchone()
    if exists:
        return {'status': 'duplicate', 'memory_id': exists['id']}
    memory = SQLiteBackend(Store(home)).propose(proposal, scope, 'deepseek_batch')
    return {'status': 'pending_review', 'memory_id': memory.memory_id,
            'association_suggestions': len(memory.duplicate_candidates), 'conflict_suggestions': len(memory.conflicts)}


def run_batch(home, manifest, confirmation):
    batch_id = manifest["preview_id"]
    count = len(manifest["entries"])
    if (not manifest.get("selected_topic") or not re.fullmatch(r"[0-9a-f]{64}", batch_id)
            or confirmation != f"确认发送批次 {batch_id}"
            or not 1 <= count <= MAX_EXPANDED_SAMPLES or count != manifest["request_count"]):
        raise ValueError("批次确认或数量校验失败")
    requests = reopen(home, manifest)
    scope = Scope(**dict(zip(Scope.model_fields, manifest["scope"], strict=True)))
    folder = home / "review_queue/v03b/batches"
    folder.mkdir(parents=True, exist_ok=True)
    journal = folder / f"{batch_id}.json"
    with portalocker.Lock(str(journal.with_suffix(".lock")), timeout=0):
        state = json.loads(journal.read_text(encoding="utf-8")) if journal.exists() else {
            "batch_id": batch_id, "started_at": now(),
            "items": [{"number": i + 1, "status": "pending"} for i in range(count)],
        }
        if state["batch_id"] != batch_id or len(state["items"]) != count:
            raise ValueError("批次进度文件与发送预览不匹配")
        if not any(item["status"] == "pending" for item in state["items"]):
            return state
        key = read_deepseek_key()
        if not key:
            raise ValueError("ZeroMemory 的 DeepSeek 凭据未配置")
        with httpx.Client(timeout=45, trust_env=False, follow_redirects=False) as client:
            for body, entry, result in zip(requests, manifest["entries"], state["items"], strict=True):
                if result["status"] != "pending":
                    continue
                result["status"] = "sent_or_uncertain"
                atomic_json(journal, state)
                try:
                    with client.stream("POST", manifest["destination"], json=body,
                                       headers={"Authorization": "Bearer " + key}) as response:
                        result["http_status"] = response.status_code
                        if response.status_code != 200:
                            result["status"] = "http_error"
                            atomic_json(journal, state)
                            break
                        raw = bytearray()
                        for chunk in response.iter_bytes(chunk_size=65536):
                            raw.extend(chunk)
                            if len(raw) > 2_000_000:
                                raise ValueError("response too large")
                    value = json.loads(raw)["choices"][0]["message"]["content"]
                    result.update(save_candidate(home, manifest, scope, body, entry, value))
                except httpx.HTTPError:
                    result["status"] = "transport_uncertain"
                    atomic_json(journal, state)
                    break
                except (ValueError, KeyError, TypeError, IndexError):
                    result["status"] = "invalid_output"
                except (OSError, sqlite3.Error, portalocker.exceptions.LockException):
                    result["status"] = "local_candidate_error"
                    atomic_json(journal, state)
                    break
                atomic_json(journal, state)
        return state


def main():
    parser = argparse.ArgumentParser(description="执行或查看已经预览的 DeepSeek 批次")
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--confirm")
    mode.add_argument("--status", action="store_true")
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        if args.status:
            reopen(args.home, manifest)
            journal = args.home / "review_queue/v03b/batches" / (manifest["preview_id"] + ".json")
            state = json.loads(journal.read_text(encoding="utf-8")) if journal.exists() else {
                "items": [{"status": "pending"} for _ in manifest["entries"]],
            }
        else:
            state = run_batch(args.home, manifest, args.confirm)
        counts = Counter(item["status"] for item in state["items"])
        print(dumps({"batch_id": manifest["preview_id"], "counts": counts}))
    except (ValueError, KeyError, OSError, portalocker.exceptions.LockException):
        parser.exit(1, "批次校验或本地操作失败。请检查预览、确认文本、凭据和本地进度。\n")


if __name__ == "__main__":
    main()

