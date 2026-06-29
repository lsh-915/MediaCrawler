import csv
import json
from pathlib import Path

from douyin_scraper.completeness import (
    filter_collectable_search_outputs,
    load_video_completeness_index,
    update_video_completeness_index,
    write_task_completeness_report,
)


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_filter_collectable_search_outputs_low_likes_and_complete_history(tmp_path: Path) -> None:
    workspace = tmp_path / "workspaces" / "aaaaaaaaaaaa"
    outputs = workspace / "outputs"
    rows = [
        {
            "source_keyword": "parking",
            "platform": "douyin",
            "video_id": "low",
            "aweme_id": "low",
            "title": "low",
            "liked_count": "100",
            "aweme_url": "https://www.douyin.com/video/low",
        },
        {
            "source_keyword": "parking",
            "platform": "douyin",
            "video_id": "new",
            "aweme_id": "new",
            "title": "new",
            "liked_count": "600",
            "aweme_url": "https://www.douyin.com/video/new",
        },
        {
            "source_keyword": "parking",
            "platform": "douyin",
            "video_id": "complete",
            "aweme_id": "complete",
            "title": "complete",
            "liked_count": "700",
            "aweme_url": "https://www.douyin.com/video/complete",
        },
        {
            "source_keyword": "parking",
            "platform": "douyin",
            "video_id": "incomplete",
            "aweme_id": "incomplete",
            "title": "incomplete",
            "liked_count": "800",
            "aweme_url": "https://www.douyin.com/video/incomplete",
        },
    ]
    fieldnames = list(rows[0])
    _write_csv(outputs / "search_result.csv", fieldnames, rows)
    _write_jsonl(outputs / "search_result.jsonl", rows)
    _write_csv(outputs / "script_sources.csv", fieldnames, rows)
    _write_jsonl(outputs / "script_sources.jsonl", rows)
    index_path = tmp_path / "data" / "cache" / "video_completeness_index.jsonl"
    _write_jsonl(
        index_path,
        [
            {"aweme_id": "complete", "is_complete": True, "latest_complete_task_id": "old"},
            {"aweme_id": "incomplete", "is_complete": False},
        ],
    )

    stats = filter_collectable_search_outputs(
        outputs,
        min_likes_threshold=500,
        index_path=index_path,
    )

    assert stats["videos_total_from_search"] == 4
    assert stats["videos_filtered_low_likes"] == 1
    assert stats["skipped_already_complete"] == 1
    assert stats["new_videos_to_collect"] == 1
    assert stats["incomplete_videos_to_repair"] == 1
    kept = list(csv.DictReader(open(outputs / "search_result.csv", encoding="utf-8-sig")))
    assert [row["aweme_id"] for row in kept] == ["new", "incomplete"]
    sources = list(csv.DictReader(open(outputs / "script_sources.csv", encoding="utf-8-sig")))
    assert [row["aweme_id"] for row in sources] == ["new", "incomplete"]
    filtered = list(csv.DictReader(open(outputs / "filtered_videos.csv", encoding="utf-8-sig")))
    assert {row["filter_reason"] for row in filtered} == {"low_likes", "already_complete"}
    assert (outputs / "search_result_all.csv").exists()


def test_update_video_completeness_index_marks_only_complete_latest(tmp_path: Path) -> None:
    workspace = tmp_path / "workspaces" / "bbbbbbbbbbbb"
    outputs = workspace / "outputs"
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
        ["video_id", "aweme_id", "asr_status", "asr_raw_text", "asr_engine"],
        [{"video_id": "video-1", "aweme_id": "video-1", "asr_status": "available", "asr_raw_text": "real", "asr_engine": "faster_whisper"}],
    )
    _write_csv(
        outputs / "script_clean.csv",
        ["video_id", "aweme_id", "script_clean_text", "script_clean_source"],
        [{"video_id": "video-1", "aweme_id": "video-1", "script_clean_text": "real", "script_clean_source": "asr"}],
    )
    _write_jsonl(
        outputs / "content_asset.jsonl",
        [{**search_row, "script_clean_text": "real", "script_clean_source": "asr", "comment_data_status": "available", "asr_data_status": "available"}],
    )

    report = write_task_completeness_report(workspace, task_id="bbbbbbbbbbbb")
    index_path = tmp_path / "data" / "cache" / "video_completeness_index.jsonl"
    update_video_completeness_index(workspace, task_id="bbbbbbbbbbbb", index_path=index_path, report=report)

    index = load_video_completeness_index(index_path)
    assert index["video-1"]["is_complete"] is True
    assert index["video-1"]["latest_complete_task_id"] == "bbbbbbbbbbbb"
    assert index["video-1"]["liked_count"] == 900
