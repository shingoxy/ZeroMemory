from scripts.acceptance_demo import run_demo


def test_real_restart_two_models_actual_mcp_and_restore(tmp_path):
    report = run_demo(tmp_path)
    assert all(value == "pass" for value in report["steps"].values())
    assert report["models"][0]["memory_id"] == report["models"][1]["memory_id"]
