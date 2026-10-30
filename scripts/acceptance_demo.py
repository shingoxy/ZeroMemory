"""Synthetic, real-process acceptance: import/review/restart/two providers/MCP/restore.

Never reads personal exports, never downloads weights, never accesses an external model.
"""

import argparse
import asyncio
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LocalService:
    def __init__(self, home, port=None):
        self.home = home
        self.port = port or free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.process = None
        self.service_pid = None

    def __enter__(self):
        env = {
            **os.environ,
            "PYTHONUTF8": "1",
            "ZERO_HOME": str(self.home),
            "ZERO_HOST": "127.0.0.1",
            "ZERO_LLM_PROVIDER": "none",
            "ZERO_ALLOW_CLOUD": "false",
            "ZERO_PORT": str(self.port),
        }
        self.process = subprocess.Popen(
            [sys.executable, "-m", "zero_memory", "--home", str(self.home), "--port", str(self.port)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(150):
            if self.process.poll() is not None:
                raise RuntimeError("Acceptance service exited during startup")
            try:
                response = httpx.get(self.url + "/health", timeout=0.3, trust_env=False)
                if response.status_code == 200:
                    self.service_pid = response.json()["process_id"]
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        self.__exit__(None, None, None)
        raise RuntimeError("Acceptance service startup timed out")

    def __exit__(self, *args):
        # Windows venv python.exe is a redirector: Popen.pid can be a waiting launcher,
        # not the interpreter that owns the listening socket. Stop the observed server PID.
        if self.service_pid:
            try:
                os.kill(self.service_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if self.process and self.process.poll() is None:
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for _ in range(30):
            with socket.socket() as sock:
                sock.settimeout(0.2)
                if sock.connect_ex(("127.0.0.1", self.port)) != 0:
                    return
            time.sleep(0.1)
        raise RuntimeError("Acceptance service still listens after termination")

    @contextmanager
    def client(self):
        with httpx.Client(
            base_url=self.url, timeout=30, trust_env=False, headers={"X-Zero-Request": "1"}
        ) as client:
            client.get("/").raise_for_status()
            yield client


class MockModelA:
    @staticmethod
    def answer(context):
        return "模型 A 收到同一份上下文。" + context


class MockModelB:
    @staticmethod
    def answer(context):
        return "模型 B 的表达不同，检索证据一致。" + context


class MockModels:
    def __enter__(self):
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                data = json.dumps({"data": [{"id": "model-a"}, {"id": "model-b"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(body)
                text = "\n".join(x["content"] for x in body["messages"])
                ids = re.findall(r'"memory_id": "([\w-]+)"', text)
                content = json.dumps({"agent_id": "zero-001", "memory_ids": ids}, ensure_ascii=False)
                model = MockModelA if body["model"] == "model-a" else MockModelB
                data = json.dumps(
                    {"choices": [{"message": {"content": model.answer(content)}}]}, ensure_ascii=False
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def checked(response):
    response.raise_for_status()
    return response.json()


async def verify_mcp(base, token, expected_id, writable=False):
    env = {
        **os.environ,
        "PYTHONUTF8": "1",
        "ZERO_API_URL": base,
        "ZERO_API_TOKEN": token,
        "ZERO_MCP_READ_ONLY": "false" if writable else "true",
        "ZERO_MCP_USER_ID": "default-user",
        "ZERO_MCP_PROJECT_ID": "default",
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "zero_memory.mcp.server"], env=env)
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = [t.name for t in listed.tools]
            assert len(names) == (20 if writable else 12)
            identity = await session.call_tool("zero_get_identity", {})
            assert not identity.isError
            search = await session.call_tool("zero_search_memories", {"query": "星舟"})
            assert not search.isError
            payload = json.loads(next(c.text for c in search.content if c.type == "text"))
            assert payload["items"][0]["memory_id"] == expected_id
            goals = await session.call_tool("zero_list_goals", {})
            assert not goals.isError
            state = await session.call_tool("zero_get_persona_state", {})
            assert not state.isError
            observatory = await session.call_tool("zero_get_observatory", {"query": "星舟"})
            assert not observatory.isError
            graph = json.loads(next(c.text for c in observatory.content if c.type == "text"))
            assert graph["memories"][0]["id"] == expected_id
            if writable:
                proposal = await session.call_tool(
                    "zero_record_experience",
                    {"content": "合成测试：完成星舟验收", "evidence": "这是合成客户端测试。"},
                )
                assert not proposal.isError
                body = json.loads(next(c.text for c in proposal.content if c.type == "text"))
                assert body["status"] == "pending_review"
                goal_result = await session.call_tool(
                    "zero_propose_goal",
                    {
                        "title": "合成 MCP 持续目标",
                        "why": "验证跨客户端持久化",
                        "acceptance_criteria": ["人工核对本地回执"],
                        "next_step": "等待人工批准",
                    },
                )
                assert not goal_result.isError
                goal = json.loads(next(c.text for c in goal_result.content if c.type == "text"))
                assert goal["status"] == "proposed"
                episode = await session.call_tool(
                    "zero_record_episode",
                    {
                        "goal_id": goal["id"],
                        "source_id": "synthetic-mcp-event",
                        "observations": "模型自述只作待核实记录",
                    },
                )
                assert not episode.isError
                recorded = json.loads(next(c.text for c in episode.content if c.type == "text"))
                assert recorded["status"] == "unverified" and not recorded["executed"]
                denied = await session.call_tool(
                    "zero_update_goal",
                    {
                        "goal_id": goal["id"],
                        "expected_version": goal["version"],
                        "reason": "模型不能批准自己",
                        "status": "ready",
                    },
                )
                assert denied.isError
            return {"tools": names, "search_memory_id": expected_id, "actual_stdio_call": True}


def run_demo(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    run = output / ("demo-" + str(uuid4()))
    home = run / "data"
    report = {"synthetic_only": True, "external_model_requests": 0, "steps": {}}
    with LocalService(home) as service, service.client() as client:
        first_port, first_pid = service.port, service.service_pid
        synthetic = [
            {
                "conversation_id": "demo-starship",
                "message_id": "user-project",
                "role": "user",
                "timestamp": "2026-10-29T00:00:00Z",
                "content": "我正在开发星舟项目，目标是让本地记忆可追溯。",
            }
        ]
        job = checked(
            client.post(
                "/v1/imports",
                files={"file": ("synthetic.json", json.dumps(synthetic, ensure_ascii=False).encode())},
            )
        )
        checked(client.post(f"/v1/imports/{job['id']}/start", json={}))
        for _ in range(60):
            state = checked(client.get(f"/v1/imports/{job['id']}"))
            if state["status"] == "completed":
                break
            time.sleep(0.1)
        assert state["inserted"] == 1
        raw = checked(client.get("/v1/messages"))["items"][0]
        memory = checked(client.post("/v1/extractions/quote/" + raw["key"], json={}))
        memory = checked(client.post("/v1/memories/" + memory["memory_id"] + "/approve", json={}))
        identity = checked(client.get("/v1/identity"))
        identity["soul"]["traits"]["humor"] = 67
        checked(
            client.patch(
                "/v1/identity",
                json={
                    "authorized": True,
                    "expected_version": 1,
                    "soul": identity["soul"],
                    "style": identity["style"],
                },
            )
        )
        report["steps"]["import_review_persona"] = "pass"
    # The first OS process was terminated. Start a new OS process with the same data directory.
    with LocalService(home, port=first_port) as service, service.client() as client, MockModels() as mocks:
        assert service.service_pid != first_pid
        report["restart"] = {
            "same_port": first_port,
            "first_server_pid": first_pid,
            "second_server_pid": service.service_pid,
            "old_listener_closed": True,
        }
        assert checked(client.get("/v1/identity"))["soul"]["traits"]["humor"] == 67
        report["steps"]["os_process_restart"] = "pass"
        results = []
        for name in ("model-a", "model-b"):
            token = checked(client.post("/v1/clients", json={"name": name}))["token"]
            with httpx.Client(
                base_url=service.url, trust_env=False, headers={"Authorization": "Bearer " + token}
            ) as model_client:
                found = checked(model_client.post("/v1/memories/search", json={"query": "星舟"}))["items"][0]
                assert found["memory_id"] == memory["memory_id"] and found["evidence"] == memory["evidence"]
                assert checked(model_client.get("/v1/identity"))["soul"]["agent_id"] == "zero-001"
            checked(
                client.patch("/v1/model", json={"provider": "openai", "model": name, "base_url": mocks.url})
            )
            answer = checked(client.post("/v1/chat", json={"message": "星舟", "session_id": name}))
            assert answer["memory_ids"] == [memory["memory_id"]] and answer["identity_version"] == 2
            results.append(
                {
                    "provider": name,
                    "memory_id": found["memory_id"],
                    "evidence": found["evidence"],
                    "agent_id": "zero-001",
                    "identity_version": answer["identity_version"],
                }
            )
            if name == "model-a":
                report["mcp_readonly"] = asyncio.run(verify_mcp(service.url, token, memory["memory_id"]))
        writer = checked(client.post("/v1/clients", json={"name": "synthetic-writer", "writable": True}))[
            "token"
        ]
        report["mcp_writer"] = asyncio.run(verify_mcp(service.url, writer, memory["memory_id"], True))
        assert len(mocks.requests) == 2
        assert all("zero-001" in r["messages"][0]["content"] for r in mocks.requests)
        report["models"] = results
        report["steps"]["two_mock_providers_same_identity_memory_evidence"] = "pass"
        backup = checked(client.post("/v1/backup"))
        archive = client.get(backup["download_url"]).content
        restored = checked(client.post("/v1/restore", files={"file": ("backup.zip", archive)}))
        with LocalService(Path(restored["restored_home"])) as recovered, recovered.client() as fresh:
            found = checked(fresh.post("/v1/memories/search", json={"query": "星舟"}))["items"][0]
            assert found["memory_id"] == memory["memory_id"] and found["evidence"] == memory["evidence"]
            assert checked(fresh.get("/v1/identity"))["soul"]["traits"]["humor"] == 67
        report["steps"]["fresh_directory_restore_and_start"] = "pass"
        report["steps"]["mcp_discovery_and_actual_read_write"] = "pass"
        report["data_home"] = str(home)
        report["restored_home"] = restored["restored_home"]
        report["memory_id"] = memory["memory_id"]
    destination = output / "acceptance-demo.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("output"))
    args = parser.parse_args()
    result = run_demo(args.output.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
