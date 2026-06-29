import csv
from pathlib import Path

from douyin_scraper.completeness import (
    select_incomplete_videos,
    write_task_completeness_report,
)


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_completeness_marks_title_fallback_scripts_incomplete(tmp_path: Path) -> None:
    workspace = tmp_path / "workspaces" / "aaaaaaaaaaaa"
    outputs = workspace / "outputs"
    search_rows = [
        {
            "source_keyword": "parking",
            "platform": "douyin",
            "video_id": f"video-{idx}",
            "aweme_id": f"video-{idx}",
            "aweme_url": f"https://www.douyin.com/video/video-{idx}",
            "title": f"title {idx}",
            "desc": "",
            "liked_count": "1",
            "collected_count": "1",
            "comment_count": "1",
            "share_count": "1",
        }
        for idx in range(1, 4)
    ]
    _write_csv(outputs / "search_result.csv", list(search_rows[0]), search_rows)
    _write_csv(
        outputs / "comments_video_status.csv",
        [
            "aweme_id",
            "video_url",
            "aweme_url",
            "status",
            "comments_collected",
            "target_comments",
        ],
        [
            {
                "aweme_id": row["aweme_id"],
                "video_url": row["aweme_url"],
                "aweme_url": row["aweme_url"],
                "status": "success",
                "comments_collected": "200",
                "target_comments": "200",
            }
            for row in search_rows
        ],
    )
    _write_csv(
        outputs / "script_raw.csv",
        [
            "video_id",
            "aweme_id",
            "asr_status",
            "asr_raw_text",
            "asr_engine",
            "download_error",
        ],
        [
            {
                "video_id": "video-1",
                "aweme_id": "video-1",
                "asr_status": "available",
                "asr_raw_text": "real transcript",
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
        [
            "video_id",
            "aweme_id",
            "script_clean_text",
            "script_clean_source",
            "script_clean_notes",
        ],
        [
            {
                "video_id": "video-1",
                "aweme_id": "video-1",
                "script_clean_text": "real transcript",
                "script_clean_source": "asr",
                "script_clean_notes": "",
            },
            {
                "video_id": "video-2",
                "aweme_id": "video-2",
                "script_clean_text": "title 2",
                "script_clean_source": "source_clean_title",
                "script_clean_notes": "fallback_source_clean_title",
            },
            {
                "video_id": "video-3",
                "aweme_id": "video-3",
                "script_clean_text": "title 3",
                "script_clean_source": "source_clean_title",
                "script_clean_notes": "fallback_source_clean_title",
            },
        ],
    )
    asset_rows = [
        {
            **row,
            "script_clean_text": "real transcript" if row["aweme_id"] == "video-1" else row["title"],
            "script_clean_source": "asr" if row["aweme_id"] == "video-1" else "source_clean_title",
            "comment_data_status": "available",
            "asr_data_status": "available" if row["aweme_id"] == "video-1" else "fallback_title",
        }
        for row in search_rows
    ]
    (outputs / "content_asset.jsonl").write_text(
        "".join(__import__("json").dumps(row, ensure_ascii=False) + "\n" for row in asset_rows),
        encoding="utf-8",
    )

    report = write_task_completeness_report(workspace, task_id="aaaaaaaaaaaa")

    assert report["videos_total"] == 3
    assert report["dimensions"]["comments"] == {"complete": 3, "incomplete": 0}
    assert report["dimensions"]["scripts"] == {"complete": 1, "incomplete": 2}
    assert report["dimensions"]["content_asset"] == {"complete": 1, "incomplete": 2}
    assert report["incomplete_reasons"]["max_script_raw_items_limit"] == 2
    assert report["incomplete_reasons"]["source_clean_title"] == 2
    assert select_incomplete_videos(report, ["scripts"]) == ["video-2", "video-3"]
    assert select_incomplete_videos(report, ["scripts"], force=True) == [
        "video-1",
        "video-2",
        "video-3",
    ]
    assert (outputs / "completeness_report.json").exists()
    assert (outputs / "completeness_video_status.csv").exists()


def test_completeness_marks_partial_comments_incomplete(tmp_path: Path) -> None:
    workspace = tmp_path / "workspaces" / "bbbbbbbbbbbb"
    outputs = workspace / "outputs"
    search_row = {
        "source_keyword": "parking",
        "platform": "douyin",
        "video_id": "video-1",
        "aweme_id": "video-1",
        "aweme_url": "https://www.douyin.com/video/video-1",
        "title": "title 1",
        "liked_count": "1",
    }
    _write_csv(outputs / "search_result.csv", list(search_row), [search_row])
    _write_csv(
        outputs / "comments_video_status.csv",
        ["aweme_id", "status", "comments_collected", "target_comments"],
        [
            {
                "aweme_id": "video-1",
                "status": "partial",
                "comments_collected": "20",
                "target_comments": "200",
            }
        ],
    )
    _write_csv(
        outputs / "script_raw.csv",
        ["video_id", "aweme_id", "asr_status", "asr_raw_text", "asr_engine"],
        [
            {
                "video_id": "video-1",
                "aweme_id": "video-1",
                "asr_status": "available",
                "asr_raw_text": "real transcript",
                "asr_engine": "faster_whisper",
            }
        ],
    )
    _write_csv(
        outputs / "script_clean.csv",
        ["video_id", "aweme_id", "script_clean_text", "script_clean_source"],
        [
            {
                "video_id": "video-1",
                "aweme_id": "video-1",
                "script_clean_text": "real transcript",
                "script_clean_source": "asr",
            }
        ],
    )
    (outputs / "content_asset.jsonl").write_text(
        __import__("json").dumps(
            {
                **search_row,
                "script_clean_text": "real transcript",
                "script_clean_source": "asr",
                "comment_data_status": "partial",
                "asr_data_status": "available",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    report = write_task_completeness_report(workspace, task_id="bbbbbbbbbbbb")

    assert report["dimensions"]["comments"] == {"complete": 0, "incomplete": 1}
    assert report["dimensions"]["scripts"] == {"complete": 1, "incomplete": 0}
    assert report["dimensions"]["content_asset"] == {"complete": 0, "incomplete": 1}
    assert report["incomplete_reasons"]["comments_partial"] == 1
    assert report["incomplete_reasons"]["content_asset_comments_incomplete"] == 1
