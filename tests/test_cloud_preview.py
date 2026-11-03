import json
import zipfile

import httpx
import pytest

from zero_memory.extraction import cloud_batch
from zero_memory.extraction.cloud_preview import prepare, reopen
from zero_memory.importers.openai_preview import file_sha256

from .conftest import import_data, propose
from .test_openai_preview import node
from .test_raw_history import prepared, run


def test_pilot_manifest_is_read_only_and_bound_to_exact_eligible_request(tmp_path, app):
    store = app.state.store
    eligible = 'I prefer notebooks with blue covers.'
    sensitive = 'My password is SYNTHETIC_SECRET.'
    assistant = 'Synthetic assistant text for exclusion.'
    source = tmp_path / 'synthetic.zip'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('conversations.json', json.dumps([
            {'id': 'c1', 'title': '创作', 'current_node': 'answer', 'mapping': {
                'u': node(None, ['answer'], 'user', eligible),
                'answer': node('u', [], 'assistant', assistant),
            }},
            {'id': 'c2', 'title': '其他', 'current_node': 'secret', 'mapping': {
                'secret': node(None, [], 'user', sensitive),
            }},
        ]))
    assert run(store, prepared(store, source))['status'] == 'completed'
    before = file_sha256(store.path)
    preview = prepare(store.home)
    assert preview['request_count'] == 1
    assert prepare(store.home) == preview
    assert file_sha256(store.path) == before
    assert all(value not in json.dumps(preview) for value in (eligible, sensitive, assistant))
    assert json.loads(reopen(store.home, preview)[0]['messages'][1]['content']) == {'untrusted_user_text': eligible}
    with pytest.raises(ValueError, match='binding mismatch'):
        reopen(store.home, {**preview, 'model': 'changed'})

    follow_up = 'I used to keep paper notes but switched to plain text files.'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('conversations.json', json.dumps([
            {'id': cid, 'current_node': 'u', 'mapping': {'u': node(None, [], 'user', text)}}
            for cid, text in [('c1', 'An updated message in an already sent conversation.'),
                              ('copy', eligible), ('c3', follow_up)]
        ]))
    assert run(store, prepared(store, source))['status'] == 'completed'
    expanded = prepare(store.home, previous=preview)
    assert expanded['excluded_conversations'] == 1 and expanded['request_count'] == 1
    assert expanded['entries'][0]['selection_signals']['time_change_wording']
    assert json.loads(reopen(store.home, expanded)[0]['messages'][1]['content']) == {'untrusted_user_text': follow_up}


def test_review_filter_uses_pilot_note_without_searching_memory_text(client):
    text = 'I prefer notebooks with blue covers.'
    import_data(client, [{'id': 'c', 'current_node': 'u', 'mapping': {'u': node(None, [], 'user', text)}}],
                'conversations.json')
    message = client.get('/v1/messages').json()['items'][0]
    pilot = propose(client, 'Prefers blue notebooks', source_type='chatgpt_export', evidence=text,
                    source_conversation_id=message['conversation_id'], source_message_ids=[message['message_id']],
                    source_message_keys=[message['key']], review_note='DeepSeek 试点 synthetic')
    propose(client, 'DeepSeek 试点 in text only')
    params = {'status': 'pending_review', 'review_origin': 'deepseek_pilot', 'limit': 1}
    response = client.post('/v1/memories/search', json=params)
    assert [m['memory_id'] for m in response.json()['items']] == [pilot['memory_id']]
    token = client.post('/v1/clients', json={'name': 'synthetic'}).json()['token']
    assert client.post('/v1/memories/search', json={**params, 'status': 'approved'},
                       headers={'Authorization': 'Bearer ' + token}).status_code == 403


def test_confirmed_topic_batch_resumes_unsent_items_and_quarantines_sensitive(tmp_path, app, monkeypatch):
    home = app.state.store.home
    texts = ["I prefer paper for technical diagrams.", "I document work decisions after each milestone."]
    source = tmp_path / "engineering.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("conversations.json", json.dumps([
            {"id": str(i), "title": "工程工具", "current_node": "u",
             "mapping": {"u": node(None, [], "user", value)}}
            for i, value in enumerate(texts)
        ]))
    run(app.state.store, prepared(app.state.store, source))
    manifest = prepare(home, topic="工程")
    calls = []
    client_factory = httpx.Client

    def request(req):
        data = json.loads(req.content)
        text = json.loads(data["messages"][1]["content"])["untrusted_user_text"]
        calls.append(text)
        if len(calls) == 1:
            raise httpx.ReadTimeout("synthetic disconnect")
        candidate = {"content": "Sensitive synthetic candidate", "evidence": text,
                     "memory_type": "user_profile", "sensitivity": "sensitive"}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"memories": [candidate]})}}]})

    monkeypatch.setattr(cloud_batch.httpx, "Client",
                        lambda **kwargs: client_factory(**kwargs, transport=httpx.MockTransport(request)))
    monkeypatch.setattr(cloud_batch, "read_deepseek_key", lambda: "fake-key")
    with pytest.raises(ValueError, match="批次确认"):
        cloud_batch.run_batch(home, manifest, "wrong")
    assert calls == []
    confirmation = "确认发送批次 " + manifest["preview_id"]
    stopped = cloud_batch.run_batch(home, manifest, confirmation)
    assert [i["status"] for i in stopped["items"]] == ["transport_uncertain", "pending"]
    finished = cloud_batch.run_batch(home, manifest, confirmation)
    assert [i["status"] for i in finished["items"]] == ["transport_uncertain", "sensitive_pending"]
    assert cloud_batch.run_batch(home, manifest, confirmation) == finished
    assert len(set(calls)) == len(calls) == 2
    with app.state.store.read() as db:
        assert db.execute("SELECT count(*) FROM memories").fetchone()[0] == 0
    assert len(list((home / "review_queue/v03b/sensitive").glob("*.json"))) == 1
