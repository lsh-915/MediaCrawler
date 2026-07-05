import csv
import time
from pathlib import Path

from api.routes import data_completeness, set_task_manager
from api.tasks import TaskManager


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_incomplete_outputs(outputs: Path) -> None:
    search_row = {
        "source_keyword": "parking",
        "platform": "douyin",
        "video_id": "video-1",
        "aweme_id": "video-1",
        "aweme_url": "https://www.douyin.com/video/video-1",
        "title": "title 1",
        "liked_count": "900",
    }
    _write_csv(outputs / "search_result.csv", list(search_row), [search_row])
    _write_csv(
        outputs / "comments_video_status.csv",
        ["aweme_id", "status", "comments_collected", "target_comments"],
        [{"aweme_id": "video-1", "status": "success", "comments_collected": "200", "target_comments": "200"}],
    )
    _write_csv(
        outputs / "script_raw.csv",
        ["video_id", "aweme_id", "asr_status", "asr_raw_text", "asr_engine", "download_error"],
        [{"video_id": "video-1", "aweme_id": "video-1", "asr_status": "skipped", "asr_raw_text": "", "asr_engine": "faster_whisper", "download_error": "max_script_raw_items_limit"}],
    )
    _write_csv(
        outputs / "script_clean.csv",
        ["video_id", "aweme_id", "script_clean_text", "script_clean_source", "script_clean_notes"],
        [{"video_id": "video-1", "aweme_id": "video-1", "script_clean_text": "title 1", "script_clean_source": "source_clean_title", "script_clean_notes": "fallback_source_clean_title"}],
    )


def _seed_incomplete_task(tmp_path: Path) -> tuple[TaskManager, str]:
    manager = TaskManager(base_dir=str(tmp_path / "workspaces"))
    task = manager.create_task("run_all", params={"max_count": 1})
    task.status = "completed"
    outputs = Path(task.workspace) / "outputs"
    _write_incomplete_outputs(outputs)
    manager._attach_data_quality(task, {})
    return manager, task.task_id


def test_task_status_includes_incomplete_data_quality(tmp_path: Path) -> None:
    manager, task_id = _seed_incomplete_task(tmp_path)
    task = manager.get_task(task_id)

    assert task is not None
    payload = task.to_dict()
    assert payload["data_quality_status"] == "incomplete"
    assert payload["data_quality_message"] == "采集不全，请补全"
    assert payload["repair_available"] is True
    assert payload["recommended_repair_dimensions"] == ["scripts", "content_asset"]


def test_data_completeness_endpoint_returns_repair_hint(tmp_path: Path) -> None:
    manager, task_id = _seed_incomplete_task(tmp_path)
    set_task_manager(manager)

    import asyncio

    report = asyncio.run(data_completeness(task_id=task_id))

    assert report["data_quality_status"] == "incomplete"
    assert report["message"] == "采集不全，请补全"
    assert report["repair_available"] is True
    assert report["recommended_repair_dimensions"] == ["scripts", "content_asset"]
    task = manager.get_task(task_id)
    assert task is not None
    assert task.to_dict()["data_quality_status"] == "incomplete"
    assert (tmp_path / "data" / "cache" / "video_completeness_index.jsonl").exists()


def test_failed_run_all_keeps_repair_hint(tmp_path: Path) -> None:
    manager = TaskManager(base_dir=str(tmp_path / "workspaces"))
    task = manager.create_task("run_all", params={"max_count": 1})

    def fail_after_partial_outputs() -> None:
        outputs = Path(task.workspace) / "outputs"
        search_row = {
            "source_keyword": "parking",
            "platform": "douyin",
            "video_id": "video-1",
            "aweme_id": "video-1",
            "aweme_url": "https://www.douyin.com/video/video-1",
            "title": "title 1",
            "liked_count": "900",
        }
        _write_csv(outputs / "search_result.csv", list(search_row), [search_row])
        _write_csv(
            outputs / "comments_video_status.csv",
            ["aweme_id", "status", "comments_collected", "target_comments"],
            [{"aweme_id": "video-1", "status": "success", "comments_collected": "200", "target_comments": "200"}],
        )
        raise RuntimeError("install_whisper failed")

    manager.submit(task, fail_after_partial_outputs)

    for _ in range(50):
        current = manager.get_task(task.task_id)
        if current and current.status == "failed":
            break
        time.sleep(0.05)

    current = manager.get_task(task.task_id)
    assert current is not None
    payload = current.to_dict()
    assert payload["status"] == "failed"
    assert payload["repair_available"] is True
    assert payload["recommended_repair_dimensions"] == ["scripts", "content_asset"]
    assert payload["result"]["data_quality_status"] == "incomplete"


def test_interrupted_resume_keeps_repair_hint(tmp_path: Path) -> None:
    manager = TaskManager(base_dir=str(tmp_path / "workspaces"))
    manager.GRACEFUL_SHUTDOWN_TIMEOUT = 0
    task = manager.create_task("resume", params={"source_task_id": "source-task"})
    task.status = "running"
    _write_incomplete_outputs(Path(task.workspace) / "outputs")

    manager._mark_running_tasks_interrupted("test")

    current = manager.get_task(task.task_id)
    assert current is not None
    payload = current.to_dict()
    assert payload["status"] == "failed"
    assert payload["exit_code"] == 4
    assert payload["repair_available"] is True
    assert payload["recommended_repair_dimensions"] == ["scripts", "content_asset"]
    assert payload["result"]["data_quality_status"] == "incomplete"
