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
PLATFORM_TEXT_SCRIPT_SOURCES = {"source_clean_title", "source_title_desc", "source_desc"}
PLATFORM_TEXT_SCRIPT_MIN_CHARS = 80
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
DEFAULT_REGION_TERMS = {
    "北京", "上海", "广州", "深圳", "杭州", "南京", "成都", "重庆", "武汉", "西安",
    "天津", "苏州", "郑州", "长沙", "青岛", "厦门", "佛山", "东莞", "合肥", "昆明",
    "驾校", "考场", "车管所", "本地", "同城", "附近",
}
MANDARIN_LANGUAGE_VALUES = {"", "zh", "zh-cn", "zh_cn", "chinese", "cn", "mandarin"}
DIALECT_HINTS = {
    "粤语", "广东话", "白话", "港式", "闽南语", "台语", "四川话", "重庆话", "东北话",
    "上海话", "吴语", "客家话", "河南话", "陕西话", "湖南话", "方言",
}


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


def _optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _bool_status(value: str) -> bool:
    return value == "complete"


def _is_complete_platform_text_script(source: str, text: str, asr_status: str) -> bool:
    return (
        asr_status == "no_audio"
        and source in PLATFORM_TEXT_SCRIPT_SOURCES
        and len(text) >= PLATFORM_TEXT_SCRIPT_MIN_CHARS
    )


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


def _liked_count(row: Dict[str, Any]) -> Optional[int]:
    for key in ("liked_count", "likes", "digg_count", "like_count"):
        if key in row:
            return _optional_int(row.get(key))
    return None


def _duration_value(row: Dict[str, Any]) -> Optional[int]:
    for key in ("video_duration", "duration", "duration_ms", "video_duration_ms"):
        if key in row:
            return _optional_int(row.get(key))
    return None


def _is_truthy_text(value: Any) -> bool:
    return _lower(value) in {"1", "true", "yes", "y", "on", "video"}


def _load_region_terms(region_terms_path: Optional[Path] = None) -> List[str]:
    candidates = []
    if region_terms_path is not None:
        candidates.append(Path(region_terms_path))
    candidates.append(Path(__file__).parent / "filters" / "region_terms.txt")
    for path in candidates:
        if not path.exists():
            continue
        terms = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if terms:
            return terms
    return sorted(DEFAULT_REGION_TERMS, key=len, reverse=True)


def _region_title_matches(row: Dict[str, Any], terms: Sequence[str]) -> List[str]:
    title = " ".join(
        _text(row.get(key))
        for key in ("clean_title", "title", "raw_title", "desc", "clean_desc")
        if _text(row.get(key))
    )
    return [term for term in terms if term and term in title]


def _filter_row(
    row: Dict[str, Any],
    *,
    stage: str,
    reason: str,
    detail: str = "",
    threshold: Any = "",
) -> Dict[str, Any]:
    return {
        **row,
        "filter_stage": stage,
        "filter_reason": reason,
        "filter_detail": detail,
        "threshold": threshold,
    }


def _video_filter_reason(row: Dict[str, Any]) -> tuple[str, str]:
    aweme_url = _lower(row.get("aweme_url") or row.get("video_url") or row.get("url"))
    download_url = _lower(
        row.get("video_download_url")
        or row.get("download_url")
        or row.get("video_url")
        or row.get("play_addr")
    )
    item_type = _lower(row.get("aweme_type") or row.get("item_type") or row.get("type"))
    is_video = row.get("is_video")

    if "/note/" in aweme_url or item_type in {"note", "image", "images", "图文"}:
        return "filtered_note_post", "note_or_image_post"
    if is_video is not None and not _is_truthy_text(is_video):
        return "filtered_not_video", "is_video=false"
    if aweme_url and "/video/" not in aweme_url and "douyin.com" in aweme_url:
        return "filtered_not_video", aweme_url
    if not (download_url or aweme_url):
        return "filtered_missing_video_url", "video url missing"
    if ".mp3" in download_url or "music" in download_url:
        return "filtered_audio_only", download_url
    duration = _duration_value(row)
    if duration is not None and duration <= 0:
        return "filtered_zero_duration", str(duration)
    return "", ""


def _is_mandarin_script_row(row: Dict[str, Any], min_text_length: int) -> tuple[bool, str, str]:
    language = _lower(row.get("language") or row.get("asr_language") or row.get("detected_language"))
    if language not in MANDARIN_LANGUAGE_VALUES:
        return False, "filtered_asr_language_not_zh", language
    text = _text(
        row.get("asr_raw_text")
        or row.get("asr_text")
        or row.get("script_raw_text")
        or row.get("script_text")
    )
    if len(text) < min_text_length:
        return False, "filtered_asr_too_short", f"length={len(text)}"
    matched = [term for term in DIALECT_HINTS if term in text]
    if matched:
        return False, "filtered_non_mandarin", ",".join(sorted(matched))
    return True, "", ""


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
    if _is_complete_platform_text_script(clean_source, clean_text, asr_status):
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
    if _is_complete_platform_text_script(source, text, _lower(clean_row.get("asr_status"))):
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
    if (
        source
        and source not in REAL_SCRIPT_SOURCES
        and not _is_complete_platform_text_script(
            source,
            script_text,
            _lower(asset_row.get("asr_data_status")),
        )
    ):
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
    enable_region_title_filter: bool = True,
    skip_already_complete: bool = True,
    region_terms_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Filter search/script source outputs before comments/scripts/content_asset collection."""
    output = Path(output_dir)
    search_csv = output / "search_result.csv"
    search_jsonl = output / "search_result.jsonl"
    empty_files = {
        "filtered_videos_csv": str(output / "filtered_videos.csv"),
        "filtered_videos_jsonl": str(output / "filtered_videos.jsonl"),
        "eligible_videos_csv": str(output / "eligible_videos.csv"),
        "eligible_videos_jsonl": str(output / "eligible_videos.jsonl"),
        "skipped_videos_csv": str(output / "skipped_videos.csv"),
        "skipped_videos_jsonl": str(output / "skipped_videos.jsonl"),
    }
    if not search_csv.exists():
        return {
            "min_likes_threshold": min_likes_threshold,
            "videos_total_from_search": 0,
            "videos_filtered_low_likes": 0,
            "videos_after_likes_filter": 0,
            "skipped_already_complete": 0,
            "new_videos_to_collect": 0,
            "incomplete_videos_to_repair": 0,
            "eligible_videos": 0,
            "filter_applied": False,
            "eligibility": {},
            "files": empty_files,
        }

    search_rows = _read_csv(search_csv)
    search_json_rows = _read_jsonl(search_jsonl)
    title_index = _index_rows(_read_csv(output / "search_title_clean.csv"))
    index = load_video_completeness_index(index_path or _default_index_path(output.parent))
    region_terms = _load_region_terms(region_terms_path)
    filtered: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    kept: List[Dict[str, Any]] = []
    skipped_complete = 0
    incomplete_history = 0
    new_count = 0

    for row in search_rows:
        key = _text(row.get("aweme_id")) or _text(row.get("video_id"))
        video_reason, video_detail = _video_filter_reason(row)
        if video_reason:
            filtered.append(_filter_row(row, stage="video_type", reason=video_reason, detail=video_detail))
            continue
        likes = _liked_count(row)
        if min_likes_threshold > 0 and likes is None:
            filtered.append(_filter_row(
                row,
                stage="likes",
                reason="filtered_missing_likes",
                threshold=min_likes_threshold,
            ))
            continue
        if min_likes_threshold > 0 and likes is not None and likes < min_likes_threshold:
            filtered.append(_filter_row(
                row,
                stage="likes",
                reason="filtered_low_likes",
                detail=f"liked_count={likes}",
                threshold=min_likes_threshold,
            ))
            continue
        if enable_region_title_filter:
            title_row = _find_match(title_index, row) if title_index else {}
            matched_terms = _region_title_matches({**row, **title_row}, region_terms)
            if matched_terms:
                filtered.append(_filter_row(
                    row,
                    stage="region_title",
                    reason="filtered_region_title",
                    detail=",".join(matched_terms),
                ))
                continue
        indexed = index.get(key) if key else None
        if indexed and indexed.get("is_complete") and skip_already_complete and not force:
            skipped_complete += 1
            skipped.append({
                **row,
                "skip_stage": "history",
                "skip_reason": "already_complete",
                "latest_complete_task_id": indexed.get("latest_complete_task_id", ""),
                "last_complete_at": indexed.get("last_complete_at", ""),
            })
            continue
        if indexed and not indexed.get("is_complete"):
            incomplete_history += 1
        else:
            new_count += 1
        kept.append({**row, "eligibility_status": "eligible"})

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
    search_fieldnames = list(search_rows[0].keys() if search_rows else [])
    _write_csv(search_csv, kept, search_fieldnames)

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
    skipped_csv = output / "skipped_videos.csv"
    skipped_jsonl = output / "skipped_videos.jsonl"
    eligible_csv = output / "eligible_videos.csv"
    eligible_jsonl = output / "eligible_videos.jsonl"
    eligible_fieldnames = list(dict.fromkeys(search_fieldnames + ["eligibility_status"]))
    _write_csv(eligible_csv, kept, eligible_fieldnames)
    _write_jsonl(eligible_jsonl, kept)
    if filtered:
        fieldnames = list(dict.fromkeys(
            list(search_rows[0].keys() if search_rows else [])
            + ["filter_stage", "filter_reason", "filter_detail", "threshold"]
        ))
        _write_csv(filtered_csv, filtered, fieldnames)
        _write_jsonl(filtered_jsonl, filtered)
    else:
        _write_csv(filtered_csv, [], ["aweme_id", "video_id", "liked_count", "filter_stage", "filter_reason", "filter_detail", "threshold"])
        _write_jsonl(filtered_jsonl, [])
    if skipped:
        skipped_fields = list(dict.fromkeys(
            search_fieldnames + ["skip_stage", "skip_reason", "latest_complete_task_id", "last_complete_at"]
        ))
        _write_csv(skipped_csv, skipped, skipped_fields)
        _write_jsonl(skipped_jsonl, skipped)
    else:
        _write_csv(skipped_csv, [], ["aweme_id", "video_id", "skip_stage", "skip_reason", "latest_complete_task_id", "last_complete_at"])
        _write_jsonl(skipped_jsonl, [])

    reason_counts = Counter(_text(row.get("filter_reason")) for row in filtered)
    eligibility = {
        "videos_from_search": len(search_rows),
        "eligible_videos": len(kept),
        "filtered_total": len(filtered),
        "skipped_already_complete": skipped_complete,
        "filtered_not_video": reason_counts.get("filtered_not_video", 0),
        "filtered_note_post": reason_counts.get("filtered_note_post", 0),
        "filtered_missing_video_url": reason_counts.get("filtered_missing_video_url", 0),
        "filtered_zero_duration": reason_counts.get("filtered_zero_duration", 0),
        "filtered_audio_only": reason_counts.get("filtered_audio_only", 0),
        "filtered_low_likes": reason_counts.get("filtered_low_likes", 0),
        "filtered_missing_likes": reason_counts.get("filtered_missing_likes", 0),
        "filtered_region_title": reason_counts.get("filtered_region_title", 0),
    }

    stats = {
        "min_likes_threshold": min_likes_threshold,
        "videos_total_from_search": len(search_rows),
        "videos_filtered_low_likes": reason_counts.get("filtered_low_likes", 0),
        "videos_after_likes_filter": len(search_rows) - reason_counts.get("filtered_low_likes", 0) - reason_counts.get("filtered_missing_likes", 0),
        "skipped_already_complete": skipped_complete,
        "new_videos_to_collect": new_count,
        "incomplete_videos_to_repair": incomplete_history,
        "videos_to_collect": len(kept),
        "filtered_total": len(filtered),
        "eligible_videos": len(kept),
        "filter_applied": True,
        "eligibility": eligibility,
        "files": {
            "filtered_videos_csv": str(filtered_csv),
            "filtered_videos_jsonl": str(filtered_jsonl),
            "eligible_videos_csv": str(eligible_csv),
            "eligible_videos_jsonl": str(eligible_jsonl),
            "skipped_videos_csv": str(skipped_csv),
            "skipped_videos_jsonl": str(skipped_jsonl),
            "search_result_all_csv": str(output / "search_result_all.csv"),
            "search_result_all_jsonl": str(output / "search_result_all.jsonl"),
        },
    }
    (output / "collection_filter_stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return stats


def filter_script_raw_mandarin_outputs(
    output_dir: Path,
    *,
    min_asr_text_length: int = 30,
    enabled: bool = True,
) -> Dict[str, Any]:
    """Filter ASR/script_raw rows before script_clean/content_asset generation."""
    output = Path(output_dir)
    raw_csv = output / "script_raw.csv"
    raw_jsonl = output / "script_raw.jsonl"
    if not enabled or not raw_csv.exists():
        return {
            "enabled": enabled,
            "script_raw_rows": 0,
            "filtered_non_mandarin": 0,
            "filtered_asr_too_short": 0,
            "filtered_asr_language_not_zh": 0,
            "eligible_script_raw_rows": 0,
            "filter_applied": False,
        }

    raw_rows = _read_csv(raw_csv)
    kept: List[Dict[str, Any]] = []
    filtered: List[Dict[str, Any]] = []
    kept_keys: set[str] = set()
    for row in raw_rows:
        ok, reason, detail = _is_mandarin_script_row(row, min_asr_text_length)
        key = _text(row.get("aweme_id")) or _text(row.get("video_id"))
        if ok:
            kept.append(row)
            if key:
                kept_keys.add(key)
        else:
            filtered.append(_filter_row(row, stage="asr", reason=reason, detail=detail))

    raw_fieldnames = list(raw_rows[0].keys() if raw_rows else [])
    original_raw_csv = output / "script_raw_all.csv"
    if not original_raw_csv.exists():
        raw_csv.replace(original_raw_csv)
    _write_csv(raw_csv, kept, raw_fieldnames)
    raw_json_rows = _read_jsonl(raw_jsonl)
    if raw_json_rows:
        original_raw_jsonl = output / "script_raw_all.jsonl"
        if not original_raw_jsonl.exists():
            raw_jsonl.replace(original_raw_jsonl)
        else:
            raw_jsonl.unlink(missing_ok=True)
        _write_jsonl(
            raw_jsonl,
            [
                row for row in raw_json_rows
                if (_text(row.get("aweme_id")) or _text(row.get("video_id"))) in kept_keys
            ],
        )

    for stem in ("script_sources", "search_result", "eligible_videos"):
        csv_path = output / f"{stem}.csv"
        jsonl_path = output / f"{stem}.jsonl"
        if csv_path.exists():
            rows = _read_csv(csv_path)
            original_csv = output / f"{stem}_before_asr_filter.csv"
            if not original_csv.exists():
                csv_path.replace(original_csv)
            _write_csv(
                csv_path,
                [
                    row for row in rows
                    if (_text(row.get("aweme_id")) or _text(row.get("video_id"))) in kept_keys
                ],
                rows[0].keys() if rows else [],
            )
        if jsonl_path.exists():
            rows = _read_jsonl(jsonl_path)
            original_jsonl = output / f"{stem}_before_asr_filter.jsonl"
            if not original_jsonl.exists():
                jsonl_path.replace(original_jsonl)
            else:
                jsonl_path.unlink(missing_ok=True)
            _write_jsonl(
                jsonl_path,
                [
                    row for row in rows
                    if (_text(row.get("aweme_id")) or _text(row.get("video_id"))) in kept_keys
                ],
            )

    filtered_csv = output / "filtered_videos.csv"
    filtered_jsonl = output / "filtered_videos.jsonl"
    existing_filtered = _read_csv(filtered_csv) if filtered_csv.exists() else []
    merged_filtered = existing_filtered + filtered
    filtered_fields = list(dict.fromkeys(
        [key for row in merged_filtered for key in row.keys()]
        or ["aweme_id", "video_id", "filter_stage", "filter_reason", "filter_detail", "threshold"]
    ))
    _write_csv(filtered_csv, merged_filtered, filtered_fields)
    _write_jsonl(filtered_jsonl, merged_filtered)

    stats_path = output / "collection_filter_stats.json"
    stats = _read_json(stats_path)
    reason_counts = Counter(_text(row.get("filter_reason")) for row in filtered)
    eligibility = dict(stats.get("eligibility") or {})
    eligibility["eligible_videos"] = len(kept)
    eligibility["filtered_total"] = _safe_int(eligibility.get("filtered_total")) + len(filtered)
    for reason in ("filtered_non_mandarin", "filtered_asr_too_short", "filtered_asr_language_not_zh"):
        eligibility[reason] = _safe_int(eligibility.get(reason)) + reason_counts.get(reason, 0)
    stats["eligibility"] = eligibility
    stats["eligible_videos"] = len(kept)
    stats["filtered_total"] = _safe_int(stats.get("filtered_total")) + len(filtered)
    stats["asr_mandarin_filter"] = {
        "enabled": True,
        "script_raw_rows": len(raw_rows),
        "eligible_script_raw_rows": len(kept),
        "filtered_non_mandarin": reason_counts.get("filtered_non_mandarin", 0),
        "filtered_asr_too_short": reason_counts.get("filtered_asr_too_short", 0),
        "filtered_asr_language_not_zh": reason_counts.get("filtered_asr_language_not_zh", 0),
    }
    stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    return stats["asr_mandarin_filter"]


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
