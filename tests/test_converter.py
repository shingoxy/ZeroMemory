import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

S = Path(__file__).resolve().parents[1] / "tools" / "convert_chatgpt_export.py"
spec = importlib.util.spec_from_file_location("converter", S)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ExportTests(unittest.TestCase):
    def test_branch_and_roles(self):
        c = {
            "id": "c1",
            "title": "你好",
            "current_node": "a2",
            "mapping": {
                "u1": {
                    "parent": None,
                    "message": {
                        "id": "u1",
                        "author": {"role": "user"},
                        "content": {"parts": ["我是用户"]},
                        "create_time": 1,
                    },
                },
                "a2": {
                    "parent": "u1",
                    "message": {
                        "id": "a2",
                        "author": {"role": "assistant"},
                        "content": {"parts": ["你好"]},
                        "create_time": 2,
                    },
                },
                "other": {
                    "parent": "u1",
                    "message": {
                        "id": "other",
                        "author": {"role": "assistant"},
                        "content": {"parts": ["另一个分支"]},
                        "create_time": 3,
                    },
                },
            },
        }
        out, policy = module.normalize_conversation(c, "conversations.json")
        self.assertEqual([x["content"] for x in out], ["我是用户", "你好"])
        self.assertEqual(policy, "active_branch")
        self.assertEqual(out[1]["can_be_user_fact"], False)

    def test_json_run(self):
        with tempfile.TemporaryDirectory() as d:
            inp = Path(d) / "conversations.json"
            out = Path(d) / "normalized.jsonl"
            inp.write_text(
                json.dumps(
                    [
                        {
                            "id": "id1",
                            "title": "A",
                            "messages": [
                                {"id": "u", "role": "user", "content": {"parts": ["测试消息"]}},
                                {"id": "s", "role": "system", "content": {"parts": ["不可导入"]}},
                                {"id": "a", "role": "assistant", "content": {"parts": ["回答"]}},
                            ],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            stats, report = module.run(inp, out)
            self.assertEqual(stats["messages"], 2)
            self.assertTrue(report.exists())
            self.assertNotIn("不可导入", out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()


class ExportZipTests(unittest.TestCase):
    def test_zip_numbered_export(self):
        import zipfile

        with tempfile.TemporaryDirectory() as d:
            zpath = Path(d) / "export.zip"
            out = Path(d) / "out.jsonl"
            conversations = [
                {
                    "id": "conv-2",
                    "title": "New",
                    "mapping": {
                        "r": {"parent": None, "message": None},
                        "u": {
                            "parent": "r",
                            "message": {
                                "id": "u",
                                "author": {"role": "user"},
                                "content": {"parts": ["ZIP测试"]},
                            },
                        },
                    },
                    "current_node": "u",
                }
            ]
            with zipfile.ZipFile(zpath, "w") as z:
                z.writestr("archive/conversations-001.json", json.dumps(conversations, ensure_ascii=False))
                z.writestr("archive/unrelated.json", "{}")
            stats, _ = module.run(zpath, out)
            self.assertEqual(stats["messages"], 1)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["content"], "ZIP测试")
