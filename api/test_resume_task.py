import asyncio
import csv
import json
import time
from pathlib import Path
from typing import Any

from api.routes import ResumeRequest, resume_task, set_task_manager
from api.tasks import TaskManager


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _seed_incomplete_task(tmp_path: Path) -> tuple[TaskManager, str]:
    manager = TaskManager(base_dir=str(tmp_path))
    task = manager.create_task("run_all", params={"max_count": 3})
    task.status = "completed"
    outputs = Path(task.workspace) / "outputs"
    search_rows = [
        {
            "platform": "douyin",
            "video_id": f"video-{idx}",
            "aweme_id": f"video-{idx}",
            "aweme_url": f"https://www.douyin.com/video/video-{idx}",
            "title": f"title {idx}",
            "liked_count": "1",
        }
        for idx in range(1, 4)
    ]
    _write_csv(outputs / "search_result.csv", list(search_rows[0]), search_rows)
    _write_csv(
        outputs / "comments_video_status.csv",
        ["aweme_id", "status", "comments_collected", "target_comments"],
        [
            {
                "aweme_id": row["aweme_id"],
                "status": "success",
                "comments_collected": "200",
                "target_comments": "200",
            }
            for row in search_rows
        ],
    )
    _write_csv(
        outputs / "script_raw.csv",
        ["video_id", "aweme_id", "asr_status", "asr_raw_text", "asr_engine", "download_error"],
        [
            {
                "video_id": "video-1",
                "aweme_id": "video-1",
                "asr_status": "available",
                "asr_raw_text": "real",
                "asr_engine": "faster_whisper",
                "download_error": "",
            },
            {
                "video_id": "video-2",
                "aweme_id": "video-2",
                "asr_status": "skipped",
                "asr_raw_text": "",
                "asr_engine": "faster_whisper",
                "download_error": "max_script_raw_items_limit",
            },
            {
                "video_id": "video-3",
                "aweme_id": "video-3",
                "asr_status": "skipped",
                "asr_raw_text": "",
                "asr_engine": "faster_whisper",
                "download_error": "max_script_raw_items_limit",
            },
        ],
    )
    _write_csv(
        outputs / "script_clean.csv",
        ["video_id", "aweme_id", "script_clean_text", "script_clean_source"],
        [
            {
                "video_id": "video-1",
                "aweme_id": "video-1",
                "script_clean_text": "real",
                "script_clean_source": "asr",
            },
            {
                "video_id": "video-2",
                "aweme_id": "video-2",
                "script_clean_text": "title 2",
                "script_clean_source": "source_clean_title",
            },
            {
                "video_id": "video-3",
                "aweme_id": "video-3",
                "script_clean_text": "title 3",
                "script_clean_source": "source_clean_title",
            },
        ],
    )
    asset_rows = [
        {
            **row,
            "script_clean_text": "real" if row["aweme_id"] == "video-1" else row["title"],
            "script_clean_source": "asr" if row["aweme_id"] == "video-1" else "source_clean_title",
            "comment_data_status": "available",
            "asr_data_status": "available" if row["aweme_id"] == "video-1" else "fallback_title",
        }
        for row in search_rows
    ]
    (outputs / "content_asset.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in asset_rows),
        encoding="utf-8",
    )
    return manager, task.task_id


def test_resume_task_plans_only_incomplete_scripts(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    manager, source_task_id = _seed_incomplete_task(tmp_path)
    set_task_manager(manager)

    def fake_run(req: ResumeRequest, source_task: Any, repair_task: Any) -> dict:
        return {
            "resume_mode": True,
            "source_task_id": req.source_task_id,
            "repair_task_id": repair_task.task_id,
        }

    monkeypatch.setattr("api.routes._run_resume_repair", fake_run)

    response = asyncio.run(
        resume_task(
            ResumeRequest(
                source_task_id=source_task_id,
                dimensions=["scripts", "content_asset"],
                skip_complete=True,
                force=False,
            )
        )
    )

    assert response["source_task_id"] == source_task_id
    assert response["planned"]["comments"] == 0
    assert response["planned"]["scripts"] == 2
    assert response["planned"]["content_asset"] == 2
    assert response["skipped"]["comments"] == 3
    assert response["skipped"]["scripts"] == 1

    repair_task = manager.get_task(response["repair_task_id"])
    for _ in range(20):
        if repair_task and repair_task.status in {"completed", "failed"}:
            break
        time.sleep(0.05)
    assert repair_task is not None
    assert repair_task.status == "completed"


def test_resume_task_force_plans_complete_scripts_too(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    manager, source_task_id = _seed_incomplete_task(tmp_path)
    set_task_manager(manager)

    def fake_run(req: ResumeRequest, source_task: Any, repair_task: Any) -> dict:
        return {
            "resume_mode": True,
            "source_task_id": req.source_task_id,
            "repair_task_id": repair_task.task_id,
        }

    monkeypatch.setattr("api.routes._run_resume_repair", fake_run)

    response = asyncio.run(
        resume_task(
            ResumeRequest(
                source_task_id=source_task_id,
                dimensions=["scripts", "content_asset"],
                skip_complete=True,
                force=True,
            )
        )
    )

    assert response["planned"]["comments"] == 0
    assert response["planned"]["scripts"] == 3
    assert response["planned"]["content_asset"] == 3
    assert response["skipped"]["comments"] == 3
    assert response["skipped"]["scripts"] == 0
    assert response["skipped"]["content_asset"] == 0
