import csv
import json
from pathlib import Path

from douyin_scraper.completeness import (
    filter_collectable_search_outputs,
    filter_script_raw_mandarin_outputs,
)


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _read_csv(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_filter_collectable_search_outputs_video_type_likes_and_region(tmp_path: Path) -> None:
    outputs = tmp_path / "workspaces" / "task" / "outputs"
    rows = [
        {
            "aweme_id": "ok",
            "video_id": "ok",
            "title": "科目三起步技巧",
            "liked_count": "800",
            "aweme_url": "https://www.douyin.com/video/ok",
            "video_download_url": "https://cdn.example/ok.mp4",
            "duration": "12",
        },
        {
            "aweme_id": "note",
            "video_id": "note",
            "title": "图文",
            "liked_count": "900",
            "aweme_url": "https://www.douyin.com/note/note",
            "video_download_url": "https://cdn.example/note.mp4",
        },
        {
            "aweme_id": "missing-url",
            "video_id": "missing-url",
            "title": "没有地址",
            "liked_count": "900",
            "aweme_url": "",
            "video_download_url": "",
        },
        {
            "aweme_id": "zero",
            "video_id": "zero",
            "title": "零时长",
            "liked_count": "900",
            "aweme_url": "https://www.douyin.com/video/zero",
            "video_download_url": "https://cdn.example/zero.mp4",
            "duration": "0",
        },
        {
            "aweme_id": "low",
            "video_id": "low",
            "title": "低赞",
            "liked_count": "10",
            "aweme_url": "https://www.douyin.com/video/low",
            "video_download_url": "https://cdn.example/low.mp4",
        },
        {
            "aweme_id": "missing-likes",
            "video_id": "missing-likes",
            "title": "无点赞",
            "liked_count": "",
            "aweme_url": "https://www.douyin.com/video/missing-likes",
            "video_download_url": "https://cdn.example/missing-likes.mp4",
        },
        {
            "aweme_id": "region",
            "video_id": "region",
            "title": "广州科目三考场路线",
            "liked_count": "900",
            "aweme_url": "https://www.douyin.com/video/region",
            "video_download_url": "https://cdn.example/region.mp4",
        },
    ]
    fieldnames = list(rows[0])
    _write_csv(outputs / "search_result.csv", fieldnames, rows)
    _write_jsonl(outputs / "search_result.jsonl", rows)

    stats = filter_collectable_search_outputs(outputs, min_likes_threshold=500)

    assert stats["eligibility"]["eligible_videos"] == 1
    assert _read_csv(outputs / "eligible_videos.csv")[0]["aweme_id"] == "ok"
    reasons = {row["filter_reason"] for row in _read_csv(outputs / "filtered_videos.csv")}
    assert reasons == {
        "filtered_note_post",
        "filtered_missing_video_url",
        "filtered_zero_duration",
        "filtered_low_likes",
        "filtered_missing_likes",
        "filtered_region_title",
    }


def test_filter_script_raw_mandarin_outputs_removes_bad_asr_before_clean(tmp_path: Path) -> None:
    outputs = tmp_path / "workspaces" / "task" / "outputs"
    rows = [
        {
            "aweme_id": "ok",
            "video_id": "ok",
            "asr_language": "zh",
            "asr_raw_text": "普通话讲解科目三起步观察打灯换挡保持车速稳定",
        },
        {"aweme_id": "short", "video_id": "short", "asr_language": "zh", "asr_raw_text": "太短"},
        {
            "aweme_id": "en",
            "video_id": "en",
            "asr_language": "en",
            "asr_raw_text": "this is not chinese mandarin speech",
        },
        {
            "aweme_id": "dialect",
            "video_id": "dialect",
            "asr_language": "zh",
            "asr_raw_text": "这段粤语方言讲解不适合普通话作品采集",
        },
    ]
    _write_csv(outputs / "script_raw.csv", list(rows[0]), rows)
    _write_jsonl(outputs / "script_raw.jsonl", rows)
    id_rows = [{"aweme_id": row["aweme_id"], "video_id": row["video_id"]} for row in rows]
    _write_csv(outputs / "script_sources.csv", ["aweme_id", "video_id"], id_rows)
    _write_jsonl(outputs / "script_sources.jsonl", id_rows)
    _write_csv(outputs / "search_result.csv", ["aweme_id", "video_id"], id_rows)

    stats = filter_script_raw_mandarin_outputs(outputs, min_asr_text_length=10)

    assert stats["eligible_script_raw_rows"] == 1
    assert [row["aweme_id"] for row in _read_csv(outputs / "script_raw.csv")] == ["ok"]
    reasons = {row["filter_reason"] for row in _read_csv(outputs / "filtered_videos.csv")}
    assert reasons == {
        "filtered_asr_too_short",
        "filtered_asr_language_not_zh",
        "filtered_non_mandarin",
    }
