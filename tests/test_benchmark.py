import io
import json
import time
import tracemalloc
from pathlib import Path

from scripts.peak_memory import peak_rss_mib
from zero_memory.core.store import dumps


def test_10000_synthetic_messages_stream_incremental_idempotent(app, scope, tmp_path):
    source = tmp_path / "synthetic-10000.jsonl"
    with source.open("w", encoding="utf-8") as f:
        for i in range(10000):
            f.write(
                dumps(
                    {
                        "role": "user" if i % 2 == 0 else "assistant",
                        "content": f"合成星舟消息 {i}",
                        "conversation_id": f"c{i // 100}",
                        "message_id": f"m{i}",
                        "timestamp": 1700000000 + i,
                    }
                )
                + "\n"
            )
    tracemalloc.start()
    start = time.perf_counter()
    with source.open("rb") as f:
        job = app.state.importer.preview(f, source.name, scope)
    assert job["total_messages"] == 10000 and job["estimated_new"] == 10000
    app.state.importer.start(job["id"], scope)
    app.state.importer.run(job["id"], scope)
    first = time.perf_counter() - start
    final = app.state.importer.get(job["id"], scope)
    assert (
        final["status"] == "completed" and final["inserted"] == 10000 and final["waiting_extraction"] == 5000
    )
    repeat_start = time.perf_counter()
    with source.open("rb") as f:
        repeat = app.state.importer.preview(f, source.name, scope)
    assert repeat["estimated_new"] == 0
    app.state.importer.start(repeat["id"], scope)
    app.state.importer.run(repeat["id"], scope)
    repeated = app.state.importer.get(repeat["id"], scope)
    assert repeated["inserted"] == 0 and repeated["duplicates"] == 10000
    delta = io.BytesIO(
        (
            dumps(
                {
                    "role": "user",
                    "content": "增量的第10001条",
                    "message_id": "new",
                    "conversation_id": "c-new",
                }
            )
            + "\n"
        ).encode()
    )
    increment = app.state.importer.preview(delta, "increment.jsonl", scope)
    app.state.importer.start(increment["id"], scope)
    app.state.importer.run(increment["id"], scope)
    assert app.state.importer.get(increment["id"], scope)["inserted"] == 1
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 150 * 1024 * 1024
    with app.state.store.read() as db:
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 10001
    output = Path("output/benchmark.json")
    output.parent.mkdir(exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "messages": 10000,
                "first_parse_import_seconds": round(first, 3),
                "repeat_and_increment_seconds": round(time.perf_counter() - repeat_start, 3),
                "peak_python_tracemalloc_mib": round(peak / 1024**2, 3),
                "peak_process_working_set_mib": round(peak_rss_mib(), 3),
                "memory_measurement": "OS process high-water working set includes interpreter and imported test modules; tracemalloc measures Python heap only",
                "duplicates": 10000,
                "incremental_inserted": 1,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
