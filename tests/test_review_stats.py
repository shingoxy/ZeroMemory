import json
from datetime import UTC, datetime, timedelta

import pytest

from zero_memory.review.stats import review_durations
from zero_memory.schemas.models import Scope

from .conftest import approve
from .review_support import RUN_A, candidate, seed_messages


def test_review_durations_ignore_long_gaps():
    t0 = datetime(2026, 11, 3, tzinfo=UTC)
    events = [((t0 + timedelta(seconds=s)).isoformat(), f"m{i}") for i, s in enumerate([0, 30, 90, 2000, 2040])]
    assert review_durations(events) == {"m1": 30.0, "m2": 60.0, "m4": 40.0}


def _run(client):
    return next(x for x in client.get("/v1/review/runs").json()["runs"] if x["run_id"] == RUN_A)


def test_run_stats_separate_manual_batch_and_revoke(app, client):
    msgs = seed_messages(client, ["我喜欢纸质笔记", "我习惯晚上写代码", "我每周去游泳", "我在学日语"])
    approve(client, candidate(app, msgs[0]))
    rejected = candidate(app, msgs[1])
    client.post(f"/v1/memories/{rejected['memory_id']}/reject", json=Scope().model_dump())
    body = {"action": "reject", "memory_ids": [candidate(app, msgs[2])["memory_id"]]}
    confirm = client.post("/v1/review/batch/preview", json=body).json()["confirm"]
    client.post("/v1/review/batch/apply", json={**body, "confirm": confirm})
    candidate(app, msgs[3])
    client.post(f"/v1/review/runs/{RUN_A}/revoke", json={"confirm": f"确认撤销批次 {RUN_A}"})
    run = _run(client)
    assert {k: run[k] for k in ("candidates", "approved", "rejected", "batch_rejected", "run_revoked", "pending")} == {
        "candidates": 4, "approved": 1, "rejected": 1, "batch_rejected": 1, "run_revoked": 1, "pending": 0}
    assert run["pass_rate"] == pytest.approx(1 / 3) and run["reject_rate"] == pytest.approx(2 / 3)


def test_run_stats_include_journal_counts(app, client):
    candidate(app, seed_messages(client, ["我喜欢纸质笔记"])[0])
    journal = app.state.store.home / "review_queue/v03b/batches" / f"{RUN_A}.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({"batch_id": RUN_A, "items": [
        {"number": 1, "status": "pending_review"},
        {"number": 2, "status": "invalid_output", "reason": "not_json"}]}), encoding="utf-8")
    assert _run(client)["journal"] == {
        "requests": 2, "statuses": {"pending_review": 1, "invalid_output": 1}, "reasons": {"not_json": 1}}
