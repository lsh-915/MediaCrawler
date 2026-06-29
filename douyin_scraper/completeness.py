"""Task-level completeness ledger for resume/repair workflows."""

from __future__ import annotations

import csv
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


REAL_SCRIPT_SOURCES = {"asr", "asr_raw", "subtitle", "caption", "ocr", "platform_caption"}
FALLBACK_SCRIPT_SOURCES = {"source_clean_title", "source_title_desc", "source_desc", "title"}
COMMENTS_COMPLETE_STATUSES = {"success", "available", "no_more_comments"}
COMMENTS_INCOMPLETE_STATUSES = {"failed", "partial", "error"}
DATA_QUALITY_MESSAGES = {
    "complete": "数据采集完整",
    "incomplete": "采集不全，请补全",
    "repairing": "正在补全缺失数据",
    "partial": "已补全部分数据，仍有缺失",
    "failed": "数据检查失败，请查看错误",
}
DEFAULT_MIN_LIKES_THRESHOLD = 500


VIDEO_STATUS_FIELDNAMES = [
    "aweme_id",
    "video_id",
    "aweme_url",
    "source_task_id",
    "source_keyword",
    "platform",
    "liked_count",
    "comment_count",
    "share_count",
    "favorite_count",
    "search_status",
    "comments_status",
    "script_raw_status",
    "script_clean_status",
    "content_asset_status",
    "is_complete",
    "missing_dimensions",
    "repair_reason",
    "comments_collected",
    "target_comments",
    "asr_status",
    "script_clean_source",
    "asset_script_source",
    "updated_at",
]


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text == "None":
        return ""
    return text


def _lower(value: Any) -> str:
    return _text(value).lower()


def _safe_int(value: Any) -> int:
    try:
        if value in (None, ""):
            return 0
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def _bool_status(value: str) -> bool:
    return value == "complete"


def _read_csv(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    with open(str(path), "r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with open(str(path), "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    return rows


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_jsonl(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_csv(path: Path, rows: Sequence[Dict[str, Any]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _load_rows(output_dir: Path, stem: str) -> List[Dict[str, Any]]:
    csv_rows = _read_csv(output_dir / f"{stem}.csv")
    if csv_rows:
        return csv_rows
    return _read_jsonl(output_dir / f"{stem}.jsonl")


def _record_keys(row: Dict[str, Any]) -> List[str]:
    keys: List[str] = []
    for key in ("aweme_id", "video_id", "aweme_url", "video_url"):
        value = _text(row.get(key))
        if value and value not in keys:
            keys.append(value)
    return keys


def _identity(row: Dict[str, Any]) -> str:
    for key in ("aweme_id", "video_id", "aweme_url"):
        value = _text(row.get(key))
        if value:
            return value
    return ""


def _index_rows(rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        for key in _record_keys(row):
            index.setdefault(key, row)
    return index


def _find_match(index: Dict[str, Dict[str, Any]], row: Dict[str, Any]) -> Dict[str, Any]:
    for key in _record_keys(row):
        match = index.get(key)
        if match is not None:
            return match
    return {}


def _search_completeness(row: Dict[str, Any]) -> tuple[str, List[str]]:
    reasons: List[str] = []
    if not (_text(row.get("aweme_id")) or _text(row.get("video_id"))):
        reasons.append("search_id_missing")
    if not (_text(row.get("aweme_url")) or _text(row.get("video_url"))):
        reasons.append("search_url_missing")
    if not (
        _text(row.get("title"))
        or _text(row.get("raw_title"))
        or _text(row.get("clean_title"))
        or _text(row.get("desc"))
        or _text(row.get("clean_desc"))
    ):
        reasons.append("search_title_desc_missing")
    if not _text(row.get("platform")):
        reasons.append("search_platform_missing")

    engagement_fields = {
        "liked_count",
        "collected_count",
        "comment_count",
        "share_count",
        "total_engagement",
        "likes",
        "favorites",
        "shares",
    }
    if not any(field in row for field in engagement_fields):
        reasons.append("search_engagement_fields_missing")
    return ("complete" if not reasons else "incomplete", reasons)


def _comments_completeness(
    status_row: Dict[str, Any],
    clean_count: int,
) -> tuple[str, List[str], int, int]:
    reasons: List[str] = []
    status = _lower(status_row.get("status") or status_row.get("comments_status"))
    collected = _safe_int(status_row.get("comments_collected"))
    target = _safe_int(status_row.get("target_comments"))

    if status in COMMENTS_INCOMPLETE_STATUSES:
        return "incomplete", [f"comments_{status}"], collected, target
    if status in COMMENTS_COMPLETE_STATUSES:
        if status == "no_more_comments" or target <= 0 or collected >= target or status == "success":
            return "complete", [], collected, target
        reasons.append("comments_below_target")
    elif clean_count > 0:
        reasons.append("comments_status_missing")
    else:
        reasons.append("comments_missing")
    return "incomplete", reasons, collected, target


def _script_raw_completeness(
    raw_row: Dict[str, Any],
    clean_row: Dict[str, Any],
) -> tuple[str, List[str], str]:
    reasons: List[str] = []
    asr_status = _lower(raw_row.get("asr_status"))
    asr_text = _text(raw_row.get("asr_raw_text") or raw_row.get("asr_text"))
    asr_engine = _text(raw_row.get("asr_engine"))
    clean_source = _lower(clean_row.get("script_clean_source"))
    clean_text = _text(clean_row.get("script_clean_text"))

    if asr_status == "available" and asr_text and asr_engine:
        return "complete", [], asr_status
    if clean_source in {"subtitle", "caption", "platform_caption"} and clean_text:
        return "complete", [], asr_status

    for field in ("download_error", "asr_error"):
        value = _text(raw_row.get(field))
        if "max_script_raw_items_limit" in value:
            reasons.append("max_script_raw_items_limit")
    if asr_status:
        reasons.append(asr_status)
    if not asr_text:
        reasons.append("script_raw_text_empty")
    if not raw_row:
        reasons.append("script_raw_missing")
    return "incomplete", _dedupe(reasons), asr_status


def _script_clean_completeness(clean_row: Dict[str, Any]) -> tuple[str, List[str], str]:
    reasons: List[str] = []
    source = _lower(clean_row.get("script_clean_source"))
    text = _text(clean_row.get("script_clean_text"))
    notes = _lower(clean_row.get("script_clean_notes"))

    if text and source in REAL_SCRIPT_SOURCES:
        return "complete", [], source
    if source:
        reasons.append(source)
    else:
        reasons.append("script_clean_missing")
    if source in FALLBACK_SCRIPT_SOURCES:
        reasons.append("fallback_script")
    if "fallback_source_clean_title" in notes:
        reasons.append("fallback_source_clean_title")
    if not text:
        reasons.append("script_clean_text_empty")
    return "incomplete", _dedupe(reasons), source


def _content_asset_completeness(
    asset_row: Dict[str, Any],
    comments_complete: bool,
) -> tuple[str, List[str], str]:
    reasons: List[str] = []
    if not asset_row:
        return "incomplete", ["content_asset_missing"], ""

    source = _lower(asset_row.get("script_clean_source"))
    script_text = _text(asset_row.get("script_clean_text") or asset_row.get("script_text"))
    for field in ("video_id", "platform"):
        if not _text(asset_row.get(field)):
            reasons.append(f"content_asset_{field}_missing")
    if not script_text:
        reasons.append("content_asset_script_text_empty")
    if source and source not in REAL_SCRIPT_SOURCES:
        reasons.append(source)
    if _lower(asset_row.get("asr_data_status")) in {"missing", "dependency_missing", "download_failed", "failed"}:
        reasons.append(f"asr_data_status_{_lower(asset_row.get('asr_data_status'))}")
    if not comments_complete:
        reasons.append("content_asset_comments_incomplete")
    return ("complete" if not reasons else "incomplete", _dedupe(reasons), source)


def _dedupe(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def data_quality_summary(report: Dict[str, Any]) -> Dict[str, Any]:
    """Return task-level quality status and recommended repair dimensions."""
    dimensions = report.get("dimensions") or {}
    videos_total = int(report.get("videos_total", 0) or 0)
    videos_incomplete = int(report.get("videos_incomplete", 0) or 0)
    recommended = [
        name
        for name in ("comments", "scripts", "content_asset")
        if int((dimensions.get(name) or {}).get("incomplete", 0) or 0) > 0
    ]
    if videos_total <= 0:
        status = "failed"
    elif videos_incomplete <= 0:
        status = "complete"
    else:
        status = "incomplete"
    return {
        "data_quality_status": status,
        "message": DATA_QUALITY_MESSAGES[status],
        "data_quality_message": DATA_QUALITY_MESSAGES[status],
        "repair_available": status == "incomplete" and bool(recommended),
        "recommended_repair_dimensions": recommended,
    }


def _comment_counts(comments_clean_rows: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in comments_clean_rows:
        for key in _record_keys(row):
            counts[key] = counts.get(key, 0) + 1
    return counts


def build_task_completeness_report(task_workspace: Path, task_id: Optional[str] = None) -> Dict[str, Any]:
    """Build an in-memory completeness report for a task workspace."""
    workspace = Path(task_workspace)
    output_dir = workspace / "outputs"
    source_task_id = task_id or workspace.name
    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    search_rows = _load_rows(output_dir, "search_result")
    if not search_rows:
        search_rows = _load_rows(output_dir, "content_asset")
    comments_status_rows = _load_rows(output_dir, "comments_video_status")
    comments_clean_rows = _load_rows(output_dir, "comments_clean")
    script_raw_rows = _load_rows(output_dir, "script_raw")
    script_clean_rows = _load_rows(output_dir, "script_clean")
    content_asset_rows = (
        _read_jsonl(output_dir / "content_asset.jsonl")
        or _read_csv(output_dir / "content_asset_full.csv")
        or _read_csv(output_dir / "content_asset.csv")
    )

    comments_status_index = _index_rows(comments_status_rows)
    raw_index = _index_rows(script_raw_rows)
    clean_index = _index_rows(script_clean_rows)
    asset_index = _index_rows(content_asset_rows)
    comments_clean_counts = _comment_counts(comments_clean_rows)

    videos: List[Dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    dimension_counts = {
        "search": {"complete": 0, "incomplete": 0},
        "comments": {"complete": 0, "incomplete": 0},
        "scripts": {"complete": 0, "incomplete": 0},
        "content_asset": {"complete": 0, "incomplete": 0},
    }

    seen: set[str] = set()
    for source_row in search_rows:
        video_key = _identity(source_row)
        if not video_key or video_key in seen:
            continue
        seen.add(video_key)
        keys = _record_keys(source_row)
        clean_count = sum(comments_clean_counts.get(key, 0) for key in keys)

        search_status, search_reasons = _search_completeness(source_row)
        comments_status, comments_reasons, comments_collected, target_comments = _comments_completeness(
            _find_match(comments_status_index, source_row),
            clean_count,
        )
        script_raw_status, raw_reasons, asr_status = _script_raw_completeness(
            _find_match(raw_index, source_row),
            _find_match(clean_index, source_row),
        )
        script_clean_status, clean_reasons, script_clean_source = _script_clean_completeness(
            _find_match(clean_index, source_row),
        )
        scripts_status = (
            "complete"
            if script_raw_status == "complete" and script_clean_status == "complete"
            else "incomplete"
        )
        content_asset_status, asset_reasons, asset_script_source = _content_asset_completeness(
            _find_match(asset_index, source_row),
            comments_status == "complete",
        )

        missing_dimensions: List[str] = []
        if search_status != "complete":
            missing_dimensions.append("search")
        if comments_status != "complete":
            missing_dimensions.append("comments")
        if scripts_status != "complete":
            missing_dimensions.append("scripts")
        if content_asset_status != "complete":
            missing_dimensions.append("content_asset")

        all_reasons = _dedupe(
            search_reasons + comments_reasons + raw_reasons + clean_reasons + asset_reasons
        )
        for reason in all_reasons:
            reason_counts[reason] += 1

        for dim, status in (
            ("search", search_status),
            ("comments", comments_status),
            ("scripts", scripts_status),
            ("content_asset", content_asset_status),
        ):
            dimension_counts[dim]["complete" if status == "complete" else "incomplete"] += 1

        videos.append({
            "aweme_id": _text(source_row.get("aweme_id")) or _text(source_row.get("video_id")),
            "video_id": _text(source_row.get("video_id")) or _text(source_row.get("aweme_id")),
            "aweme_url": _text(source_row.get("aweme_url")) or _text(source_row.get("video_url")),
            "source_task_id": source_task_id,
            "source_keyword": _text(source_row.get("source_keyword")),
            "platform": _text(source_row.get("platform")) or "douyin",
            "liked_count": _safe_int(source_row.get("liked_count") or source_row.get("likes")),
            "comment_count": _safe_int(source_row.get("comment_count") or source_row.get("comments_count")),
            "share_count": _safe_int(source_row.get("share_count") or source_row.get("shares")),
            "favorite_count": _safe_int(source_row.get("collected_count") or source_row.get("favorites")),
            "search_status": search_status,
            "comments_status": comments_status,
            "script_raw_status": script_raw_status,
            "script_clean_status": script_clean_status,
            "content_asset_status": content_asset_status,
            "is_complete": not missing_dimensions,
            "missing_dimensions": missing_dimensions,
            "repair_reason": all_reasons,
            "comments_collected": comments_collected,
            "target_comments": target_comments,
            "asr_status": asr_status,
            "script_clean_source": script_clean_source,
            "asset_script_source": asset_script_source,
            "updated_at": generated_at,
        })

    videos_complete = sum(1 for row in videos if row["is_complete"])
    report = {
        "task_id": source_task_id,
        "source_task_id": source_task_id,
        "workspace": str(workspace),
        "generated_at": generated_at,
        "videos_total": len(videos),
        "videos_complete": videos_complete,
        "videos_incomplete": len(videos) - videos_complete,
        "dimensions": dimension_counts,
        "incomplete_reasons": dict(sorted(reason_counts.items())),
        "collection_filter_stats": _read_json(output_dir / "collection_filter_stats.json"),
        "videos": videos,
    }
    report.update(data_quality_summary(report))
    return report


def _csv_value(value: Any) -> str:
    if isinstance(value, list):
        return "|".join(str(item) for item in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value if value is not None else "")


def write_task_completeness_report(task_workspace: Path, task_id: Optional[str] = None) -> Dict[str, Any]:
    """Build and persist completeness report files under the task outputs directory."""
    report = build_task_completeness_report(task_workspace, task_id=task_id)
    output_dir = Path(task_workspace) / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "completeness_report.json"
    jsonl_path = output_dir / "completeness_video_status.jsonl"
    csv_path = output_dir / "completeness_video_status.csv"

    report_for_file = dict(report)
    report_for_file["videos"] = report["videos"]
    report_path.write_text(
        json.dumps(report_for_file, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with open(str(jsonl_path), "w", encoding="utf-8") as handle:
        for row in report["videos"]:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    with open(str(csv_path), "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=VIDEO_STATUS_FIELDNAMES)
        writer.writeheader()
        for row in report["videos"]:
            writer.writerow({field: _csv_value(row.get(field, "")) for field in VIDEO_STATUS_FIELDNAMES})

    report["files"] = {
        "completeness_report": str(report_path),
        "completeness_video_status_jsonl": str(jsonl_path),
        "completeness_video_status_csv": str(csv_path),
    }
    return report


def _default_index_path(task_workspace: Path) -> Path:
    workspace = Path(task_workspace).resolve()
    if workspace.parent.name == "workspaces":
        return workspace.parent.parent / "data" / "cache" / "video_completeness_index.jsonl"
    return Path.cwd() / "data" / "cache" / "video_completeness_index.jsonl"


def load_video_completeness_index(index_path: Path) -> Dict[str, Dict[str, Any]]:
    """Load the global video completeness index keyed by aweme/video id."""
    index: Dict[str, Dict[str, Any]] = {}
    for row in _read_jsonl(Path(index_path)):
        key = _text(row.get("aweme_id")) or _text(row.get("video_id"))
        if key:
            index[key] = row
    return index


def update_video_completeness_index(
    task_workspace: Path,
    task_id: Optional[str] = None,
    index_path: Optional[Path] = None,
    report: Optional[Dict[str, Any]] = None,
) -> Path:
    """Merge a task completeness report into the global completeness index."""
    workspace = Path(task_workspace)
    target = Path(index_path) if index_path is not None else _default_index_path(workspace)
    existing = load_video_completeness_index(target)
    report_data = report or write_task_completeness_report(workspace, task_id=task_id)
    generated_at = _text(report_data.get("generated_at")) or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    current_task_id = task_id or _text(report_data.get("task_id")) or workspace.name

    for row in report_data.get("videos", []):
        key = _text(row.get("aweme_id")) or _text(row.get("video_id"))
        if not key:
            continue
        is_complete = bool(row.get("is_complete"))
        previous = dict(existing.get(key) or {})
        indexed = {
            **previous,
            "aweme_id": key,
            "video_id": _text(row.get("video_id")) or key,
            "platform": _text(row.get("platform")) or previous.get("platform") or "douyin",
            "latest_workspace": str(workspace),
            "search_complete": _bool_status(_text(row.get("search_status"))),
            "comments_complete": _bool_status(_text(row.get("comments_status"))),
            "scripts_complete": (
                _bool_status(_text(row.get("script_raw_status")))
                and _bool_status(_text(row.get("script_clean_status")))
            ),
            "content_asset_complete": _bool_status(_text(row.get("content_asset_status"))),
            "is_complete": is_complete,
            "updated_at": generated_at,
            "source_keyword": _text(row.get("source_keyword")) or previous.get("source_keyword", ""),
            "liked_count": _safe_int(row.get("liked_count") or previous.get("liked_count")),
            "comment_count": _safe_int(row.get("comment_count") or previous.get("comment_count")),
            "share_count": _safe_int(row.get("share_count") or previous.get("share_count")),
            "favorite_count": _safe_int(row.get("favorite_count") or previous.get("favorite_count")),
        }
        if is_complete:
            indexed["latest_complete_task_id"] = current_task_id
            indexed["last_complete_at"] = generated_at
        existing[key] = indexed

    rows = sorted(existing.values(), key=lambda item: str(item.get("aweme_id", "")))
    _write_jsonl(target, rows)
    return target


def filter_collectable_search_outputs(
    output_dir: Path,
    *,
    min_likes_threshold: int = DEFAULT_MIN_LIKES_THRESHOLD,
    index_path: Optional[Path] = None,
    force: bool = False,
) -> Dict[str, Any]:
    """Filter search/script source outputs before comments/scripts/content_asset collection."""
    output = Path(output_dir)
    search_csv = output / "search_result.csv"
    search_jsonl = output / "search_result.jsonl"
    if not search_csv.exists():
        return {
            "min_likes_threshold": min_likes_threshold,
            "videos_total_from_search": 0,
            "videos_filtered_low_likes": 0,
            "videos_after_likes_filter": 0,
            "skipped_already_complete": 0,
            "new_videos_to_collect": 0,
            "incomplete_videos_to_repair": 0,
            "filter_applied": False,
        }

    search_rows = _read_csv(search_csv)
    search_json_rows = _read_jsonl(search_jsonl)
    index = load_video_completeness_index(index_path or _default_index_path(output.parent))
    filtered: List[Dict[str, Any]] = []
    kept: List[Dict[str, Any]] = []
    skipped_complete = 0
    incomplete_history = 0
    new_count = 0

    for row in search_rows:
        key = _text(row.get("aweme_id")) or _text(row.get("video_id"))
        likes = _safe_int(row.get("liked_count") or row.get("likes"))
        if min_likes_threshold > 0 and likes < min_likes_threshold:
            filtered.append({
                **row,
                "filter_reason": "low_likes",
                "threshold": min_likes_threshold,
            })
            continue
        indexed = index.get(key) if key else None
        if indexed and indexed.get("is_complete") and not force:
            skipped_complete += 1
            filtered.append({
                **row,
                "filter_reason": "already_complete",
                "threshold": min_likes_threshold,
                "latest_complete_task_id": indexed.get("latest_complete_task_id", ""),
            })
            continue
        if indexed and not indexed.get("is_complete"):
            incomplete_history += 1
        else:
            new_count += 1
        kept.append(row)

    def _keep_json_row(row: Dict[str, Any]) -> bool:
        key = _text(row.get("aweme_id")) or _text(row.get("video_id"))
        return any((_text(item.get("aweme_id")) or _text(item.get("video_id"))) == key for item in kept)

    if search_json_rows:
        original_json = output / "search_result_all.jsonl"
        if not original_json.exists():
            search_jsonl.replace(original_json)
        else:
            search_jsonl.unlink(missing_ok=True)
        _write_jsonl(search_jsonl, [row for row in search_json_rows if _keep_json_row(row)])
    original_csv = output / "search_result_all.csv"
    if not original_csv.exists():
        search_csv.replace(original_csv)
    _write_csv(search_csv, kept, search_rows[0].keys() if search_rows else [])

    script_sources_csv = output / "script_sources.csv"
    script_sources_jsonl = output / "script_sources.jsonl"
    if script_sources_csv.exists():
        source_rows = _read_csv(script_sources_csv)
        kept_keys = {_text(row.get("aweme_id")) or _text(row.get("video_id")) for row in kept}
        kept_sources = [
            row for row in source_rows
            if (_text(row.get("aweme_id")) or _text(row.get("video_id"))) in kept_keys
        ]
        original_sources_csv = output / "script_sources_all.csv"
        if not original_sources_csv.exists():
            script_sources_csv.replace(original_sources_csv)
        _write_csv(script_sources_csv, kept_sources, source_rows[0].keys() if source_rows else [])
        if script_sources_jsonl.exists():
            source_json_rows = _read_jsonl(script_sources_jsonl)
            original_sources_jsonl = output / "script_sources_all.jsonl"
            if not original_sources_jsonl.exists():
                script_sources_jsonl.replace(original_sources_jsonl)
            else:
                script_sources_jsonl.unlink(missing_ok=True)
            _write_jsonl(
                script_sources_jsonl,
                [
                    row for row in source_json_rows
                    if (_text(row.get("aweme_id")) or _text(row.get("video_id"))) in kept_keys
                ],
            )

    filtered_csv = output / "filtered_videos.csv"
    filtered_jsonl = output / "filtered_videos.jsonl"
    if filtered:
        fieldnames = list(dict.fromkeys(
            list(search_rows[0].keys() if search_rows else [])
            + ["filter_reason", "threshold", "latest_complete_task_id"]
        ))
        _write_csv(filtered_csv, filtered, fieldnames)
        _write_jsonl(filtered_jsonl, filtered)
    else:
        _write_csv(filtered_csv, [], ["aweme_id", "video_id", "liked_count", "filter_reason", "threshold"])
        _write_jsonl(filtered_jsonl, [])

    stats = {
        "min_likes_threshold": min_likes_threshold,
        "videos_total_from_search": len(search_rows),
        "videos_filtered_low_likes": sum(1 for row in filtered if row.get("filter_reason") == "low_likes"),
        "videos_after_likes_filter": len(search_rows) - sum(1 for row in filtered if row.get("filter_reason") == "low_likes"),
        "skipped_already_complete": skipped_complete,
        "new_videos_to_collect": new_count,
        "incomplete_videos_to_repair": incomplete_history,
        "videos_to_collect": len(kept),
        "filtered_total": len(filtered),
        "filter_applied": True,
        "files": {
            "filtered_videos_csv": str(filtered_csv),
            "filtered_videos_jsonl": str(filtered_jsonl),
            "search_result_all_csv": str(output / "search_result_all.csv"),
            "search_result_all_jsonl": str(output / "search_result_all.jsonl"),
        },
    }
    (output / "collection_filter_stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return stats


def load_video_completeness(task_workspace: Path) -> List[Dict[str, Any]]:
    """Load persisted video completeness rows, building them if needed."""
    output_dir = Path(task_workspace) / "outputs"
    jsonl_path = output_dir / "completeness_video_status.jsonl"
    if not jsonl_path.exists():
        return write_task_completeness_report(task_workspace).get("videos", [])
    return _read_jsonl(jsonl_path)


def select_incomplete_videos(
    report: Dict[str, Any],
    dimensions: Optional[Sequence[str]] = None,
    *,
    force: bool = False,
) -> List[str]:
    """Return aweme/video ids that need repair for any requested dimension."""
    wanted = set(dimensions or ("comments", "scripts", "content_asset"))
    selected: List[str] = []
    for row in report.get("videos", []):
        missing = set(row.get("missing_dimensions") or [])
        if force or missing.intersection(wanted):
            video_id = _text(row.get("aweme_id")) or _text(row.get("video_id"))
            if video_id:
                selected.append(video_id)
    return selected
