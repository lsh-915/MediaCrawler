"""
douyin_scraper.api.routes — API 路由
======================================
v6 新增：FastAPI 路由层，对 DouyinScraper 模块的 HTTP 封装。

API 设计决策：
  1. 所有长时间操作返回 task_id，客户端轮询状态
  2. 每个任务独立 workspace，互不干扰
  3. 结果文件通过 /scrape/result/{task_id} 下载
  4. 错误响应包含 exit_code 分类（1=可重试, 2=不可重试, 3=致命）

我实际执行时踩过的坑：
  - HTTP handler 中直接运行采集 → 请求超时
  - 没有任务隔离 → 并发请求互相干扰
  - 错误只返回 500 → 客户端无法区分可重试和不可重试错误
  - 结果文件路径硬编码 → 部署后找不到文件
"""

import csv
import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from typing import Literal

from douyin_scraper import DouyinScraper, ScraperConfig
from douyin_scraper.completeness import (
    select_incomplete_videos,
    update_video_completeness_index,
    write_task_completeness_report,
)
from douyin_scraper.exceptions import (
    ConfigError,
    FatalError,
    NonRetryableError,
    RetryableError,
    ScraperError,
)
from douyin_scraper.utils import (
    check_disk_space,
    check_port_in_use,
    check_command_exists,
    setup_ffmpeg,
)

from .tasks import TaskManager
from .utils import validate_path_in_workspace
from .ws import ws_manager

logger = logging.getLogger("douyin_scraper.api")

router = APIRouter(prefix="/scrape", tags=["scrape"])

# 全局任务管理器（由 main.py 注入）
_task_manager: Optional[TaskManager] = None


def set_task_manager(tm: TaskManager) -> None:
    global _task_manager
    _task_manager = tm


def get_task_manager() -> TaskManager:
    if _task_manager is None:
        raise RuntimeError("TaskManager 未初始化")
    return _task_manager


# ═══════════════════════════════════════════════════════════════
# 请求模型
# ═══════════════════════════════════════════════════════════════

class SearchRequest(BaseModel):
    """搜索采集请求"""
    keywords: List[str] = Field(..., description="搜索关键词列表")
    max_count: int = Field(20, description="每个关键词最大采集数", ge=1, le=200)
    min_likes_threshold: int = Field(500, description="min likes threshold", ge=0)
    force: bool = Field(False, description="ignore complete history index")
    content_asset_comments_limit: int = Field(50, ge=1, le=5000)
    content_asset_full_comments_limit: int = Field(200, ge=1, le=5000)
    enable_region_title_filter: bool = Field(True)
    enable_mandarin_filter: bool = Field(True)
    skip_already_complete: bool = Field(True)
    min_asr_text_length: int = Field(30, ge=0, le=1000)
    project_dir: Optional[str] = Field(None, description="工作目录（默认自动创建）")

    @field_validator("keywords")
    @classmethod
    def validate_keywords(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("keywords 不能为空")
        if len(v) > 50:
            raise ValueError("keywords 最多 50 个")
        for kw in v:
            if len(kw) > 200:
                raise ValueError(f"关键词过长: {kw[:50]}...")
        return v


class CommentsRequest(BaseModel):
    """评论采集请求"""
    task_id: Optional[str] = Field(None, description="搜索任务 ID")
    video_ids: Optional[List[str]] = Field(None, description="直接指定视频 ID 列表")
    max_comments_per_video: int = Field(
        50, description="每个视频最多采集评论数", ge=1, le=5000
    )
    video_jsonl: Optional[str] = Field(None, description="视频 JSONL 路径")
    project_dir: Optional[str] = Field(None, description="工作目录")


class ScriptsRequest(BaseModel):
    """文案提取请求"""
    task_id: Optional[str] = Field(None, description="搜索任务 ID（读取其 script_sources 输出）")
    video_jsonl: Optional[str] = Field(None, description="视频 JSONL 路径")
    model: Literal["tiny", "base", "small", "medium", "large"] = Field(
        "small", description="Whisper 模型大小: tiny/base/small/medium/large"
    )
    project_dir: Optional[str] = Field(None, description="工作目录")


class MergeRequest(BaseModel):
    """合并数据请求"""
    search_task_id: Optional[str] = Field(None, description="搜索任务 ID（生成 content_asset）")
    comments_task_id: Optional[str] = Field(None, description="评论任务 ID（可选）")
    scripts_task_id: Optional[str] = Field(None, description="文案任务 ID（可选）")
    video_jsonl: Optional[str] = Field(None, description="视频 JSONL 路径")
    comments_jsonl: Optional[str] = Field(None, description="评论 JSONL 路径")
    scripts_jsonl: Optional[str] = Field(None, description="文案 JSONL 路径")
    output_csv: Optional[str] = Field(None, description="输出 CSV 路径")
    content_asset_comments_limit: int = Field(50, ge=1, le=5000)
    content_asset_full_comments_limit: int = Field(200, ge=1, le=5000)
    project_dir: Optional[str] = Field(None, description="工作目录")


class ResetRequest(BaseModel):
    """重置步骤请求"""
    step: str = Field(..., description="要重置的步骤名称")
    clear_dedupe: bool = Field(False, description="是否同时清除去重索引")
    project_dir: Optional[str] = Field(None, description="工作目录")

    @field_validator("step")
    @classmethod
    def validate_step(cls, v: str) -> str:
        valid_steps = {
            "clone_repo", "setup_env", "config_douyin",
            "run_search", "fetch_comments", "install_ffmpeg",
            "install_whisper", "run_extract", "merge_csv",
        }
        if v not in valid_steps:
            raise ValueError(
                f"无效步骤名: {v}，有效步骤: {', '.join(sorted(valid_steps))}"
            )
        return v


class RunAllRequest(BaseModel):
    """一键运行请求"""
    keywords: List[str] = Field(..., description="搜索关键词列表")
    max_count: int = Field(20, description="每个关键词最大采集数")
    min_likes_threshold: int = Field(500, description="min likes threshold", ge=0)
    force: bool = Field(False, description="ignore complete history index")
    content_asset_comments_limit: int = Field(50, ge=1, le=5000)
    content_asset_full_comments_limit: int = Field(200, ge=1, le=5000)
    enable_region_title_filter: bool = Field(True)
    enable_mandarin_filter: bool = Field(True)
    skip_already_complete: bool = Field(True)
    min_asr_text_length: int = Field(30, ge=0, le=1000)
    steps: Optional[List[str]] = Field(None, description="指定步骤（默认全部）")
    project_dir: Optional[str] = Field(None, description="工作目录")


# ═══════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════

class ResumeRequest(BaseModel):
    """Resume/repair an incomplete historical task."""
    source_task_id: str = Field(..., description="历史任务 ID")
    dimensions: List[Literal["comments", "scripts", "content_asset"]] = Field(
        default_factory=lambda: ["scripts", "content_asset"],
        description="需要补采的维度",
    )
    skip_complete: bool = Field(True, description="完整视频是否跳过")
    max_count: Optional[int] = Field(None, description="最多处理视频数")
    max_comments_per_video: int = Field(200, ge=1, le=5000)
    force: bool = Field(False, description="是否强制重采完整视频")
    model: Literal["tiny", "base", "small", "medium", "large"] = Field("small")
    project_dir: Optional[str] = Field(None, description="工作目录")

    @field_validator("dimensions")
    @classmethod
    def validate_dimensions(
        cls,
        value: List[Literal["comments", "scripts", "content_asset"]],
    ) -> List[Literal["comments", "scripts", "content_asset"]]:
        if not value:
            raise ValueError("dimensions 不能为空")
        return list(dict.fromkeys(value))


def _media_crawler_root() -> Path:
    """MediaCrawler repository root (contains main.py)."""
    return Path(__file__).resolve().parents[1]


def _make_scraper(project_dir: Optional[str], workspace: str) -> DouyinScraper:
    """
    创建 DouyinScraper 实例。

    ★ 我实际执行时：多个请求共享同一个 scraper 实例，
    状态互相覆盖 → 每个任务独立实例。★

    project_dir 始终为 MediaCrawler 项目根（含 main.py），
    任务数据目录由 state_dir_name=workspaces/<task_id>/state 派生。
    workspace 仅用于提取 task_id，不作为 project_dir。
    """
    if project_dir is None:
        # Docker 检测：/app/main.py 存在 → 容器环境
        if Path("/app/main.py").exists():
            project_dir = "/app/"
        else:
            project_dir = str(_media_crawler_root())

    # 每个任务使用独立的 state_dir，避免任务间状态污染
    # workspace 格式: .../workspaces/<task_id> → state_dir = workspaces/<task_id>/state
    ws_path = Path(workspace)
    task_id = ws_path.name  # 从 workspace 路径中提取 task_id
    state_dir_name = f"workspaces/{task_id}/state"

    config_dict: Dict[str, Any] = {
        "project_dir": project_dir,
        "state_dir_name": state_dir_name,
        "enable_cdp_mode": False,  # Docker 中不用 CDP，用 headless playwright
    }
    return DouyinScraper(config_dict)


def _apply_collection_options(scraper: DouyinScraper, req: Any) -> None:
    for attr in (
        "min_likes_threshold",
        "content_asset_comments_limit",
        "content_asset_full_comments_limit",
        "enable_region_title_filter",
        "enable_mandarin_filter",
        "skip_already_complete",
        "min_asr_text_length",
    ):
        if hasattr(req, attr):
            setattr(scraper.config, attr, getattr(req, attr))
    if hasattr(req, "force"):
        scraper.config.force_recollect_complete = bool(getattr(req, "force"))


def _error_response(e: Exception) -> HTTPException:
    """将异常转换为 HTTP 响应"""
    if isinstance(e, ScraperError):
        status_map = {1: 503, 2: 400, 3: 500}  # 可重试/不可重试/致命
        status_code = status_map.get(e.exit_code, 500)
        return HTTPException(
            status_code=status_code,
            detail={
                "error": str(e),
                "step": e.step,
                "exit_code": e.exit_code,
                "details": e.details,
            },
        )
    # 非 ScraperError：不暴露内部信息给 API 调用者
    logger.error("未预期异常: %s", e, exc_info=True)
    return HTTPException(
        status_code=500,
        detail={"error": "内部错误，请查看日志"},
    )


# ═══════════════════════════════════════════════════════════════
# API 端点
# ═══════════════════════════════════════════════════════════════

def _search_output_result(paths: Dict[str, Any], output: Path) -> Dict[str, Any]:
    return {
        "video_jsonl": paths.get("video_jsonl", str(output)),
        "video_csv": paths.get("video_csv", ""),
        "csv_stats": paths.get("csv_stats", {}),
        "eligible_videos_jsonl": paths.get("eligible_videos_jsonl", ""),
        "eligible_videos_csv": paths.get("eligible_videos_csv", ""),
        "filtered_videos_jsonl": paths.get("filtered_videos_jsonl", ""),
        "filtered_videos_csv": paths.get("filtered_videos_csv", ""),
        "skipped_videos_jsonl": paths.get("skipped_videos_jsonl", ""),
        "skipped_videos_csv": paths.get("skipped_videos_csv", ""),
        "collection_filter_stats": paths.get("collection_filter_stats", {}),
        "eligibility": (paths.get("collection_filter_stats", {}) or {}).get("eligibility", {}),
    }



def _title_clean_result(paths: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "title_clean_jsonl": paths.get("title_clean_jsonl", ""),
        "title_clean_csv": paths.get("title_clean_csv", ""),
        "title_clean_stats": paths.get("title_clean_stats", {}),
    }



def _script_source_result(paths: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "script_sources_jsonl": paths.get("script_sources_jsonl", ""),
        "script_sources_csv": paths.get("script_sources_csv", ""),
        "script_sources_stats": paths.get("script_sources_stats", {}),
    }



def _comments_output_result(
    paths: Dict[str, Any],
    output: Path,
) -> Dict[str, Any]:
    comments_raw_jsonl = paths.get("comments_raw_jsonl", str(output))
    return {
        "comments_jsonl": comments_raw_jsonl,
        "comments_raw_jsonl": comments_raw_jsonl,
        "comments_raw_csv": paths.get("comments_raw_csv", ""),
        "comments_clean_jsonl": paths.get("comments_clean_jsonl", ""),
        "comments_clean_csv": paths.get("comments_clean_csv", ""),
        "comments_stats": paths.get("comments_stats", {}),
        "clean_stats": paths.get("clean_stats", {}),
    }



def _script_output_result(
    paths: Dict[str, Any],
    output: Path,
) -> Dict[str, Any]:
    script_raw_jsonl = paths.get("script_raw_jsonl", str(output))
    return {
        "scripts_jsonl": script_raw_jsonl,
        "script_raw_jsonl": script_raw_jsonl,
        "script_raw_csv": paths.get("script_raw_csv", ""),
        "script_raw_stats": paths.get("script_raw_stats", {}),
        "script_clean_jsonl": paths.get("script_clean_jsonl", ""),
        "script_clean_csv": paths.get("script_clean_csv", ""),
        "script_clean_stats": paths.get("script_clean_stats", {}),
    }


def _row_keys(row: Dict[str, Any]) -> List[str]:
    keys: List[str] = []
    for key in ("aweme_id", "video_id", "aweme_url", "video_url"):
        value = str(row.get(key, "") or "").strip()
        if value and value != "None" and value not in keys:
            keys.append(value)
    return keys


def _row_matches_ids(row: Dict[str, Any], ids: set[str]) -> bool:
    return any(key in ids for key in _row_keys(row))


def _load_table_rows(csv_path: Path, jsonl_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    if csv_path.exists():
        with open(str(csv_path), "r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    if jsonl_path and jsonl_path.exists():
        rows: List[Dict[str, Any]] = []
        with open(str(jsonl_path), "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
        return rows
    return []


def _fieldnames(rows: List[Dict[str, Any]], preferred: Optional[List[str]] = None) -> List[str]:
    fields: List[str] = []
    for field in preferred or []:
        if field not in fields:
            fields.append(field)
    for row in rows:
        for field in row.keys():
            if field not in fields:
                fields.append(field)
    return fields


def _write_table_rows(
    rows: List[Dict[str, Any]],
    csv_path: Path,
    jsonl_path: Path,
    *,
    preferred_fieldnames: Optional[List[str]] = None,
) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = _fieldnames(rows, preferred_fieldnames)
    with open(str(jsonl_path), "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    with open(str(csv_path), "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _copy_output_if_exists(source_dir: Path, target_dir: Path, name: str) -> None:
    source = source_dir / name
    if source.exists():
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target_dir / name)


def _copy_reusable_outputs(source_dir: Path, target_dir: Path) -> None:
    for name in (
        "search_result.csv",
        "search_result.jsonl",
        "search_title_clean.csv",
        "search_title_clean.jsonl",
        "comments_clean.csv",
        "comments_clean.jsonl",
        "comments_video_status.csv",
        "comments_video_status.jsonl",
        "script_sources.csv",
        "script_sources.jsonl",
        "script_raw.csv",
        "script_raw.jsonl",
        "script_clean.csv",
        "script_clean.jsonl",
    ):
        _copy_output_if_exists(source_dir, target_dir, name)


def _filter_rows_by_ids(rows: List[Dict[str, Any]], ids: set[str]) -> List[Dict[str, Any]]:
    return [row for row in rows if _row_matches_ids(row, ids)]


def _combine_single_rows_by_ids(
    source_rows: List[Dict[str, Any]],
    repair_rows: List[Dict[str, Any]],
    repair_ids: set[str],
) -> List[Dict[str, Any]]:
    repair_by_key: Dict[str, Dict[str, Any]] = {}
    for row in repair_rows:
        for key in _row_keys(row):
            repair_by_key[key] = row

    combined: List[Dict[str, Any]] = []
    used: set[int] = set()
    for row in source_rows:
        replacement: Optional[Dict[str, Any]] = None
        if _row_matches_ids(row, repair_ids):
            for key in _row_keys(row):
                if key in repair_by_key:
                    replacement = repair_by_key[key]
                    used.add(id(replacement))
                    break
        combined.append(replacement or row)

    for row in repair_rows:
        if id(row) not in used:
            combined.append(row)
    return combined


def _combine_multi_rows_by_ids(
    source_rows: List[Dict[str, Any]],
    repair_rows: List[Dict[str, Any]],
    repair_ids: set[str],
) -> List[Dict[str, Any]]:
    kept = [row for row in source_rows if not _row_matches_ids(row, repair_ids)]
    return kept + repair_rows


def _video_ids_for_dimension(
    report: Dict[str, Any],
    dimension: str,
    *,
    skip_complete: bool,
    force: bool,
) -> List[str]:
    if force or not skip_complete:
        return [
            str(row.get("aweme_id") or row.get("video_id"))
            for row in report.get("videos", [])
            if row.get("aweme_id") or row.get("video_id")
        ]
    return select_incomplete_videos(report, [dimension], force=False)


def _resume_plan(
    report: Dict[str, Any],
    dimensions: List[str],
    *,
    skip_complete: bool,
    force: bool,
) -> Dict[str, Dict[str, int]]:
    total = int(report.get("videos_total", 0))
    planned: Dict[str, int] = {}
    skipped: Dict[str, int] = {}
    for dimension in ("comments", "scripts", "content_asset"):
        if dimension not in dimensions:
            planned[dimension] = 0
            skipped[dimension] = total
            continue
        count = len(_video_ids_for_dimension(
            report,
            dimension,
            skip_complete=skip_complete,
            force=force,
        ))
        planned[dimension] = count
        skipped[dimension] = max(total - count, 0)
    return {"planned": planned, "skipped": skipped}


def _ensure_repair_script_sources(
    scraper: DouyinScraper,
    repair_outputs: Path,
) -> tuple[Path, Path]:
    sources_jsonl = repair_outputs / "script_sources.jsonl"
    sources_csv = repair_outputs / "script_sources.csv"
    if sources_jsonl.exists() or sources_csv.exists():
        return sources_jsonl, sources_csv

    search_csv = repair_outputs / "search_result.csv"
    if not search_csv.exists():
        raise NonRetryableError(
            "resume source task has no search_result.csv for script repair",
            step="resume",
        )
    search_jsonl = repair_outputs / "search_result.jsonl"
    title_clean_csv = repair_outputs / "search_title_clean.csv"
    scraper._do_build_script_sources(
        search_csv,
        search_jsonl if search_jsonl.exists() else None,
        title_clean_csv if title_clean_csv.exists() else None,
    )
    return sources_jsonl, sources_csv


def _repair_comments(
    scraper: DouyinScraper,
    source_outputs: Path,
    repair_outputs: Path,
    comment_ids: List[str],
    max_comments_per_video: int,
) -> Dict[str, Any]:
    if not comment_ids:
        return {"comments_repaired": 0}

    id_set = set(comment_ids)
    search_rows = _load_table_rows(
        source_outputs / "search_result.csv",
        source_outputs / "search_result.jsonl",
    )
    input_rows = _filter_rows_by_ids(search_rows, id_set)
    repair_input = repair_outputs / "repair_input_videos.jsonl"
    _write_table_rows(
        input_rows,
        repair_outputs / "repair_input_videos.csv",
        repair_input,
    )
    comments_jsonl = scraper.fetch_comments(
        video_jsonl=repair_input,
        max_comments_per_video=max_comments_per_video,
    )
    paths = scraper.get_paths()
    repair_clean_csv = Path(paths.get("comments_clean_csv", repair_outputs / "comments_clean.csv"))
    repair_clean_jsonl = Path(paths.get("comments_clean_jsonl", repair_outputs / "comments_clean.jsonl"))
    if repair_clean_csv.exists():
        shutil.copyfile(repair_clean_csv, repair_outputs / "comments_clean_repair.csv")
    if repair_clean_jsonl.exists():
        shutil.copyfile(repair_clean_jsonl, repair_outputs / "comments_clean_repair.jsonl")
    if Path(comments_jsonl).exists():
        shutil.copyfile(Path(comments_jsonl), repair_outputs / "comments_raw_repair.jsonl")

    source_clean_rows = _load_table_rows(
        source_outputs / "comments_clean.csv",
        source_outputs / "comments_clean.jsonl",
    )
    repair_clean_rows = _load_table_rows(repair_clean_csv, repair_clean_jsonl)
    combined_clean = _combine_multi_rows_by_ids(source_clean_rows, repair_clean_rows, id_set)
    _write_table_rows(
        combined_clean,
        repair_outputs / "comments_clean.csv",
        repair_outputs / "comments_clean.jsonl",
    )
    return {
        "comments_repaired": len(comment_ids),
        "comments_jsonl": str(comments_jsonl),
        "comments_clean_csv": str(repair_outputs / "comments_clean.csv"),
    }


def _repair_scripts(
    scraper: DouyinScraper,
    source_outputs: Path,
    repair_outputs: Path,
    script_ids: List[str],
    model: str,
) -> Dict[str, Any]:
    if not script_ids:
        return {"scripts_repaired": 0}

    id_set = set(script_ids)
    full_sources_jsonl, full_sources_csv = _ensure_repair_script_sources(
        scraper,
        repair_outputs,
    )
    source_rows = _load_table_rows(full_sources_csv, full_sources_jsonl)
    repair_sources = _filter_rows_by_ids(source_rows, id_set)
    repair_sources_csv = repair_outputs / "script_sources_repair.csv"
    repair_sources_jsonl = repair_outputs / "script_sources_repair.jsonl"
    _write_table_rows(repair_sources, repair_sources_csv, repair_sources_jsonl)

    raw_jsonl, raw_csv, raw_stats = scraper._do_build_script_raw(
        script_sources_jsonl=repair_sources_jsonl,
        script_sources_csv=repair_sources_csv,
        model_name=model,
        max_items=len(repair_sources),
    )
    shutil.copyfile(raw_jsonl, repair_outputs / "script_raw_repair.jsonl")
    shutil.copyfile(raw_csv, repair_outputs / "script_raw_repair.csv")

    source_raw_rows = _load_table_rows(
        source_outputs / "script_raw.csv",
        source_outputs / "script_raw.jsonl",
    )
    repair_raw_rows = _load_table_rows(raw_csv, raw_jsonl)
    combined_raw_rows = _combine_single_rows_by_ids(source_raw_rows, repair_raw_rows, id_set)
    combined_raw_csv = repair_outputs / "script_raw.csv"
    combined_raw_jsonl = repair_outputs / "script_raw.jsonl"
    _write_table_rows(
        combined_raw_rows,
        combined_raw_csv,
        combined_raw_jsonl,
        preferred_fieldnames=scraper._script_raw_fieldnames(),
    )

    title_clean_csv = repair_outputs / "search_title_clean.csv"
    clean_jsonl, clean_csv, clean_stats = scraper._do_build_script_clean(
        script_sources_jsonl=full_sources_jsonl,
        script_sources_csv=full_sources_csv,
        script_raw_jsonl=combined_raw_jsonl,
        script_raw_csv=combined_raw_csv,
        title_clean_csv=title_clean_csv if title_clean_csv.exists() else None,
    )
    return {
        "scripts_repaired": len(script_ids),
        "script_raw_repair_jsonl": str(repair_outputs / "script_raw_repair.jsonl"),
        "script_raw_jsonl": str(combined_raw_jsonl),
        "script_raw_csv": str(combined_raw_csv),
        "script_raw_stats": raw_stats,
        "script_clean_jsonl": str(clean_jsonl),
        "script_clean_csv": str(clean_csv),
        "script_clean_stats": clean_stats,
    }


def _run_resume_repair(
    req: ResumeRequest,
    source_task: Any,
    repair_task: Any,
) -> Dict[str, Any]:
    source_workspace = Path(source_task.workspace)
    source_outputs = source_workspace / "outputs"
    repair_workspace = Path(repair_task.workspace)
    repair_outputs = repair_workspace / "outputs"
    repair_outputs.mkdir(parents=True, exist_ok=True)
    (repair_workspace / "source_task_id.txt").write_text(req.source_task_id, encoding="utf-8")

    source_report = write_task_completeness_report(
        source_workspace,
        task_id=req.source_task_id,
    )
    plan = _resume_plan(
        source_report,
        list(req.dimensions),
        skip_complete=req.skip_complete,
        force=req.force,
    )
    _copy_reusable_outputs(source_outputs, repair_outputs)

    scraper = _make_scraper(req.project_dir, repair_task.workspace)
    scraper.config.max_videos_per_keyword = req.max_count or int(source_report.get("videos_total", 0) or 1)
    scraper.config.max_script_raw_items = scraper.config.max_videos_per_keyword
    scraper.config.whisper_model = req.model

    comments_ids = _video_ids_for_dimension(
        source_report,
        "comments",
        skip_complete=req.skip_complete,
        force=req.force,
    ) if "comments" in req.dimensions else []
    script_ids = _video_ids_for_dimension(
        source_report,
        "scripts",
        skip_complete=req.skip_complete,
        force=req.force,
    ) if "scripts" in req.dimensions else []

    comments_result = _repair_comments(
        scraper,
        source_outputs,
        repair_outputs,
        comments_ids,
        req.max_comments_per_video,
    )
    scripts_result = _repair_scripts(
        scraper,
        source_outputs,
        repair_outputs,
        script_ids,
        req.model,
    )

    if "content_asset" in req.dimensions or script_ids or comments_ids:
        jsonl_path, csv_path, asset_stats = scraper.build_content_asset(
            search_outputs_dir=repair_outputs,
            comments_outputs_dir=repair_outputs,
            scripts_outputs_dir=repair_outputs,
        )
    else:
        jsonl_path = repair_outputs / "content_asset.jsonl"
        csv_path = repair_outputs / "content_asset.csv"
        asset_stats = {}

    repair_report = write_task_completeness_report(
        repair_workspace,
        task_id=repair_task.task_id,
    )
    videos_total = int(source_report.get("videos_total", 0))
    result = {
        "resume_mode": True,
        "source_task_id": req.source_task_id,
        "repair_task_id": repair_task.task_id,
        "videos_total": videos_total,
        "videos_reused": max(videos_total - len(set(comments_ids + script_ids)), 0),
        "videos_repaired": len(set(comments_ids + script_ids)),
        "comments_reused": plan["skipped"]["comments"],
        "comments_repaired": len(comments_ids),
        "scripts_reused": plan["skipped"]["scripts"],
        "scripts_repaired": len(script_ids),
        "planned": plan["planned"],
        "skipped": plan["skipped"],
        "collection_status": "repaired",
        "content_asset_jsonl": str(jsonl_path),
        "content_asset_full_csv": str(csv_path.with_name("content_asset_full.csv")),
        "content_asset_csv": str(csv_path),
        "content_asset_stats": asset_stats,
        "source_completeness": {
            "videos_total": source_report.get("videos_total", 0),
            "videos_complete": source_report.get("videos_complete", 0),
            "videos_incomplete": source_report.get("videos_incomplete", 0),
            "dimensions": source_report.get("dimensions", {}),
            "incomplete_reasons": source_report.get("incomplete_reasons", {}),
            "files": source_report.get("files", {}),
        },
        "repair_completeness": {
            "videos_total": repair_report.get("videos_total", 0),
            "videos_complete": repair_report.get("videos_complete", 0),
            "videos_incomplete": repair_report.get("videos_incomplete", 0),
            "dimensions": repair_report.get("dimensions", {}),
            "incomplete_reasons": repair_report.get("incomplete_reasons", {}),
            "files": repair_report.get("files", {}),
        },
    }
    result.update(comments_result)
    result.update(scripts_result)
    return result


# Search endpoint

@router.post("/search", summary="触发搜索采集")
async def search(req: SearchRequest) -> Dict[str, Any]:
    """
    触发搜索采集任务（异步执行）。

    返回 task_id，使用 GET /scrape/status/{task_id} 查询进度。
    """
    tm = get_task_manager()
    task = tm.create_task("search", params=req.model_dump())

    def _do_search() -> Dict[str, Any]:
        logger.info("API search request task_id=%s keywords=%r", task.task_id, req.keywords)
        scraper = _make_scraper(req.project_dir, task.workspace)
        _apply_collection_options(scraper, req)
        output = scraper.search(keywords=req.keywords, max_count=req.max_count)
        paths = scraper.get_paths()
        result = _search_output_result(paths, output)
        result.update(_title_clean_result(paths))
        result.update(_script_source_result(paths))
        result["status"] = scraper.get_status()
        return result

    tm.submit(task, _do_search)
    return {"task_id": task.task_id, "status": "submitted", "type": "search"}


@router.post("/comments", summary="触发评论采集")
async def fetch_comments(req: CommentsRequest) -> Dict[str, Any]:
    """
    触发评论采集任务（异步执行）。
    需要先完成搜索采集，或提供 video_jsonl 路径。
    """
    tm = get_task_manager()
    task = tm.create_task("comments", params=req.model_dump())

    def _do_comments() -> Dict[str, Any]:
        scraper = _make_scraper(req.project_dir, task.workspace)
        source_task_id = req.task_id
        video_path: Optional[Path] = None
        if req.task_id:
            source_task = tm.get_task(req.task_id)
            if not source_task:
                raise NonRetryableError(
                    f"搜索任务不存在: {req.task_id}",
                    step="fetch_comments",
                )
            source_outputs = (Path(source_task.workspace) / "outputs").resolve()
            csv_path = source_outputs / "search_result.csv"
            jsonl_path = source_outputs / "search_result.jsonl"
            if csv_path.exists():
                video_path = validate_path_in_workspace(str(csv_path.resolve()), source_outputs)
            elif jsonl_path.exists():
                video_path = validate_path_in_workspace(str(jsonl_path.resolve()), source_outputs)
            else:
                raise NonRetryableError(
                    f"搜索任务无可用输出: {req.task_id}",
                    step="fetch_comments",
                )
        elif req.video_jsonl:
            video_path = validate_path_in_workspace(
                req.video_jsonl, Path(task.workspace)
            )
        output = scraper.fetch_comments(
            video_jsonl=video_path,
            video_ids=req.video_ids,
            source_task_id=source_task_id,
            max_comments_per_video=req.max_comments_per_video,
        )
        paths = scraper.get_paths()
        result = _comments_output_result(paths, output)
        result["status"] = scraper.get_status()
        return result

    tm.submit(task, _do_comments)
    return {"task_id": task.task_id, "status": "submitted", "type": "comments"}


@router.post("/scripts", summary="触发言案提取")
async def extract_scripts(req: ScriptsRequest) -> Dict[str, Any]:
    """
    触发视频文案提取任务（异步执行）。
    需要先完成搜索采集，或提供 video_jsonl 路径。
    """
    tm = get_task_manager()
    task = tm.create_task("scripts", params=req.model_dump())

    def _do_scripts() -> Dict[str, Any]:
        scraper = _make_scraper(req.project_dir, task.workspace)
        script_sources_jsonl: Optional[Path] = None
        script_sources_csv: Optional[Path] = None
        title_clean_csv: Optional[Path] = None
        if req.task_id:
            source_task = tm.get_task(req.task_id)
            if not source_task:
                raise NonRetryableError(
                    f"搜索任务不存在: {req.task_id}",
                    step="extract_scripts",
                )
            try:
                source_max_count = source_task.params.get("max_count")
                if source_max_count is not None:
                    scraper.config.max_videos_per_keyword = int(source_max_count)
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid source task max_count for scripts task: task_id=%s",
                    req.task_id,
                )
            source_outputs = Path(source_task.workspace) / "outputs"
            jsonl_path = source_outputs / "script_sources.jsonl"
            csv_path = source_outputs / "script_sources.csv"
            title_clean_path = source_outputs / "search_title_clean.csv"
            if jsonl_path.exists():
                script_sources_jsonl = jsonl_path
            if csv_path.exists():
                script_sources_csv = csv_path
            if title_clean_path.exists():
                title_clean_csv = title_clean_path
            if not script_sources_jsonl and not script_sources_csv:
                raise NonRetryableError(
                    f"搜索任务无可用 script_sources 输出: {req.task_id}",
                    step="extract_scripts",
                )
        else:
            current_outputs = Path(task.workspace) / "outputs"
            jsonl_path = current_outputs / "script_sources.jsonl"
            csv_path = current_outputs / "script_sources.csv"
            title_clean_path = current_outputs / "search_title_clean.csv"
            if jsonl_path.exists():
                script_sources_jsonl = jsonl_path
            if csv_path.exists():
                script_sources_csv = csv_path
            if title_clean_path.exists():
                title_clean_csv = title_clean_path

        if script_sources_jsonl or script_sources_csv:
            output = scraper.extract_script_raw(
                script_sources_jsonl=script_sources_jsonl,
                script_sources_csv=script_sources_csv,
                model=req.model,
                title_clean_csv=title_clean_csv,
            )
            paths = scraper.get_paths()
            result = _script_output_result(paths, output)
            result["status"] = scraper.get_status()
            return result

        video_path = Path(req.video_jsonl) if req.video_jsonl else None
        if video_path:
            video_path = validate_path_in_workspace(
                req.video_jsonl, Path(task.workspace)
            )
        output = scraper.extract_scripts(
            video_jsonl=video_path, model=req.model
        )
        paths = scraper.get_paths()
        result = _script_output_result(paths, output)
        result["scripts_jsonl"] = str(output)
        result["status"] = scraper.get_status()
        return result

    tm.submit(task, _do_scripts)
    return {"task_id": task.task_id, "status": "submitted", "type": "scripts"}


@router.post("/merge", summary="触发数据合并")
async def merge(req: MergeRequest) -> Dict[str, Any]:
    """
    触发数据合并任务（异步执行）。
    合并视频、评论、文案数据生成标准 CSV。
    """
    tm = get_task_manager()
    task = tm.create_task("merge", params=req.model_dump())

    def _do_merge() -> Dict[str, Any]:
        scraper = _make_scraper(req.project_dir, task.workspace)
        _apply_collection_options(scraper, req)
        if req.search_task_id:
            search_task = tm.get_task(req.search_task_id)
            if not search_task or search_task.status != "completed":
                raise NonRetryableError(
                    f"搜索任务不可用: {req.search_task_id}",
                    step="merge_csv",
                )
            search_outputs = Path(search_task.workspace) / "outputs"
            search_csv = search_outputs / "search_result.csv"
            if not search_csv.exists():
                raise NonRetryableError(
                    f"搜索任务无 search_result.csv: {req.search_task_id}",
                    step="merge_csv",
                )

            comments_outputs: Optional[Path] = None
            if req.comments_task_id:
                comments_task = tm.get_task(req.comments_task_id)
                if not comments_task or comments_task.status != "completed":
                    raise NonRetryableError(
                        f"评论任务不可用: {req.comments_task_id}",
                        step="merge_csv",
                    )
                comments_outputs = Path(comments_task.workspace) / "outputs"

            scripts_outputs: Optional[Path] = None
            if req.scripts_task_id:
                scripts_task = tm.get_task(req.scripts_task_id)
                if not scripts_task or scripts_task.status != "completed":
                    raise NonRetryableError(
                        f"文案任务不可用: {req.scripts_task_id}",
                        step="merge_csv",
                    )
                scripts_outputs = Path(scripts_task.workspace) / "outputs"

            jsonl_path, csv_path, stats = scraper.build_content_asset(
                search_outputs_dir=search_outputs,
                comments_outputs_dir=comments_outputs,
                scripts_outputs_dir=scripts_outputs,
            )
            return {
                "content_asset_jsonl": str(jsonl_path),
                "content_asset_full_csv": str(csv_path.with_name("content_asset_full.csv")),
                "content_asset_csv": str(csv_path),
                "content_asset_stats": stats,
                "status": scraper.get_status(),
            }

        workspace = Path(task.workspace)
        v_path = Path(req.video_jsonl) if req.video_jsonl else None
        c_path = Path(req.comments_jsonl) if req.comments_jsonl else None
        s_path = Path(req.scripts_jsonl) if req.scripts_jsonl else None
        o_path = Path(req.output_csv) if req.output_csv else None
        # 路径遍历防护：验证所有用户提供的路径都在 workspace 内
        if v_path:
            v_path = validate_path_in_workspace(req.video_jsonl, workspace)
        if c_path:
            c_path = validate_path_in_workspace(req.comments_jsonl, workspace)
        if s_path:
            s_path = validate_path_in_workspace(req.scripts_jsonl, workspace)
        if o_path:
            o_path = validate_path_in_workspace(req.output_csv, workspace)
        output = scraper.merge(
            video_jsonl=v_path,
            comments_jsonl=c_path,
            scripts_jsonl=s_path,
            output_csv=o_path,
        )
        return {"csv_path": str(output), "status": scraper.get_status()}

    tm.submit(task, _do_merge)
    return {"task_id": task.task_id, "status": "submitted", "type": "merge"}


@router.post("/run-all", summary="一键执行全部步骤")
async def run_all(req: RunAllRequest) -> Dict[str, Any]:
    """
    一键执行全部采集步骤（搜索→评论→文案→合并）。
    """
    tm = get_task_manager()
    task = tm.create_task("run_all", params=req.model_dump())

    def _do_run_all() -> Dict[str, Any]:
        scraper = _make_scraper(req.project_dir, task.workspace)
        scraper.config.keywords = req.keywords
        scraper.config.max_videos_per_keyword = req.max_count
        _apply_collection_options(scraper, req)
        result = scraper.run_all(steps=req.steps)
        if result.get("error"):
            error = str(result.get("error") or "run_all failed")
            step = str(result.get("error_step") or "")
            exit_code = result.get("exit_code")
            if exit_code == 1:
                raise RetryableError(error, step=step)
            if exit_code == 3:
                raise FatalError(error, step=step)
            raise NonRetryableError(error, step=step)
        return result

    tm.submit(task, _do_run_all)
    return {"task_id": task.task_id, "status": "submitted", "type": "run_all"}


@router.post("/resume", summary="从历史任务续采缺失数据")
async def resume_task(req: ResumeRequest) -> Dict[str, Any]:
    tm = get_task_manager()
    if not tm.is_valid_task_id(req.source_task_id):
        raise HTTPException(status_code=400, detail="无效 source_task_id")
    source_task = tm.get_task(req.source_task_id)
    if not source_task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {req.source_task_id}")
    source_workspace = Path(source_task.workspace)
    if not source_workspace.exists():
        raise HTTPException(status_code=404, detail="源任务 workspace 不存在")

    source_report = write_task_completeness_report(
        source_workspace,
        task_id=req.source_task_id,
    )
    plan = _resume_plan(
        source_report,
        list(req.dimensions),
        skip_complete=req.skip_complete,
        force=req.force,
    )
    task = tm.create_task("resume", params=req.model_dump())

    def _do_resume() -> Dict[str, Any]:
        return _run_resume_repair(req, source_task, task)

    tm.submit(task, _do_resume)
    return {
        "repair_task_id": task.task_id,
        "task_id": task.task_id,
        "source_task_id": req.source_task_id,
        "status": "submitted",
        "type": "resume",
        "data_quality_status": "repairing",
        "data_quality_message": "正在补全缺失数据",
        "planned": plan["planned"],
        "skipped": plan["skipped"],
        "videos_total": source_report.get("videos_total", 0),
        "videos_complete": source_report.get("videos_complete", 0),
        "videos_incomplete": source_report.get("videos_incomplete", 0),
        "incomplete_reasons": source_report.get("incomplete_reasons", {}),
    }


@router.get("/status/{task_id}", summary="查询任务状态")
async def get_status(task_id: str) -> Dict[str, Any]:
    """查询异步任务的状态"""
    tm = get_task_manager()
    task = tm.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    return task.to_dict()


@router.get("/result/{task_id}", summary="下载结果文件")
async def get_result(task_id: str):
    """
    下载任务的结果文件（CSV 或 JSONL）。

    ★ 我实际执行时：结果文件散落在各处，用户找不到。
    v6：通过 task_id 自动定位结果文件。★
    """
    tm = get_task_manager()
    task = tm.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    if task.status != "completed":
        raise HTTPException(
            status_code=400,
            detail=f"任务未完成，当前状态: {task.status}",
        )

    result_path = tm.get_result_path(task_id)
    if not result_path:
        raise HTTPException(status_code=404, detail="结果文件不存在")

    if result_path.is_file():
        # 确定媒体类型
        media_type = "application/octet-stream"
        if result_path.suffix == ".csv":
            media_type = "text/csv"
        elif result_path.suffix == ".jsonl":
            media_type = "application/jsonl"

        filename = result_path.name
        return FileResponse(
            path=str(result_path),
            media_type=media_type,
            filename=filename,
        )
    elif result_path.is_dir():
        # T016: 优先列出 CSV 和 JSONL 文件
        all_files = [f for f in result_path.rglob("*") if f.is_file()]
        csv_files = [f for f in all_files if f.suffix == ".csv"]
        jsonl_files = [f for f in all_files if f.suffix == ".jsonl"]
        return JSONResponse(content={
            "task_id": task_id,
            "files": [f.name for f in all_files],
            "csv_files": [str(f) for f in csv_files],
            "jsonl_files": [str(f) for f in jsonl_files],
        })

    raise HTTPException(status_code=404, detail="结果路径无效")


@router.post("/reset", summary="重置步骤状态")
async def reset_step(req: ResetRequest) -> Dict[str, Any]:
    """
    重置某步骤的状态为 pending。

    ★ 我实际执行时：步骤失败后无法重新执行，只能手动删除状态文件。
    v6：通过 API 重置，可选清除去重索引。★
    """
    try:
        scraper = _make_scraper(req.project_dir, "./workspace_default")
        scraper.reset_step(req.step, clear_dedupe=req.clear_dedupe)
        return {"status": "reset", "step": req.step, "clear_dedupe": req.clear_dedupe}
    except ScraperError as e:
        raise _error_response(e)
    except Exception as e:
        raise _error_response(e)


@router.get("/tasks", summary="列出所有任务")
async def list_tasks(
    task_type: Optional[str] = Query(None, description="按类型过滤"),
    status: Optional[str] = Query(None, description="按状态过滤"),
    limit: int = Query(50, ge=1, le=200, description="返回数量上限"),
    offset: int = Query(0, ge=0, description="偏移量"),
) -> Dict[str, Any]:
    """列出任务（支持分页）"""
    tm = get_task_manager()
    all_tasks = tm.list_tasks(task_type=task_type, status=status, limit=10000)
    total = len(all_tasks)
    # 应用 offset 和 limit
    paginated = all_tasks[offset : offset + limit]
    return {
        "tasks": [t.to_dict() for t in paginated],
        "total": total,
        "offset": offset,
        "limit": limit,
        "stats": tm.get_stats(),
    }


@router.delete("/tasks/{task_id}", summary="删除任务记录")
async def delete_task(task_id: str) -> Dict[str, str]:
    """删除任务记录"""
    tm = get_task_manager()
    if not tm.is_valid_task_id(task_id):
        raise HTTPException(status_code=400, detail="无效任务 ID")
    try:
        if tm.delete_task(task_id):
            return {"status": "deleted", "task_id": task_id}
    except ValueError:
        logger.warning("拒绝删除非法任务 workspace: task_id=%s", task_id)
        raise HTTPException(status_code=400, detail="任务工作目录无效，已拒绝删除")
    except OSError:
        logger.warning("删除任务 workspace 失败: task_id=%s", task_id)
        raise HTTPException(status_code=500, detail="删除任务失败")
    raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")


@router.post("/cleanup", summary="清理过期任务")
async def cleanup_tasks(
    max_age_hours: int = Query(72, ge=1, description="保留最近 N 小时的任务"),
) -> Dict[str, Any]:
    """清理超过指定时间的已完成/失败任务"""
    tm = get_task_manager()
    removed = tm.cleanup_old_tasks(max_age_hours=max_age_hours)
    return {"removed": removed, "remaining": len(tm.list_tasks())}


# ═══════════════════════════════════════════════════════════════
# 数据管理 API
# ═══════════════════════════════════════════════════════════════

import csv
import io
import json as _json

MAX_EXPORT_ROWS = 200
MAX_EXPORT_BYTES = 2 * 1024 * 1024  # 2MB


def _find_result_files(workspace: Path) -> List[Path]:
    """在 workspace 中查找 CSV/JSONL 结果文件（最多 2 层深度）"""
    results: List[Path] = []
    for pattern in ("*.csv", "*.jsonl"):
        for p in workspace.rglob(pattern):
            if len(p.relative_to(workspace).parts) <= 2 and p.is_file():
                results.append(p)
    return results


def _count_file_rows(path: Path) -> int:
    """统计文件行数（不含空行），出错返回 0"""
    try:
        count = 0
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.strip():
                    count += 1
        return count
    except OSError:
        return 0


def _read_jsonl_rows(path: Path, limit: int = MAX_EXPORT_ROWS) -> List[Dict[str, Any]]:
    """读取 JSONL 文件，返回字典列表"""
    rows: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(_json.loads(line))
                except _json.JSONDecodeError:
                    continue
                if len(rows) >= limit:
                    break
    except OSError:
        pass
    return rows


def _read_csv_rows(path: Path, limit: int = MAX_EXPORT_ROWS) -> List[Dict[str, Any]]:
    """读取 CSV 文件，返回字典列表"""
    rows: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(dict(row))
                if len(rows) >= limit:
                    break
    except OSError:
        pass
    return rows


def _normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """规范化一行数据，确保关键字段存在"""
    def first_value(*keys: str, default: str = "") -> str:
        for key in keys:
            value = row.get(key)
            if value not in (None, ""):
                return str(value)
        return default

    return {
        "video_id": first_value("video_id", "aweme_id", "id"),
        "platform": first_value("platform", default="douyin"),
        "script_text": first_value(
            "script_text", "script_clean_text", "script", "text"
        ),
        "likes": first_value("likes", "liked_count", "like_count"),
        "favorites": first_value(
            "favorites", "collected_count", "collect_count"
        ),
        "shares": first_value("shares", "share_count"),
        "comments": first_value("comments", "comment_count"),
    }


@router.get("/data/list", summary="列出可导出的数据文件")
async def list_data_files() -> Dict[str, Any]:
    """
    扫描所有已完成任务的 workspace，返回可导出的数据文件列表。
    每条记录包含：task_id, task_type, file_name, file_size, row_count, created_at
    """
    tm = get_task_manager()
    all_tasks = tm.list_tasks(status="completed", limit=10000)

    items: List[Dict[str, Any]] = []
    for task in all_tasks:
        workspace = Path(task.workspace)
        if not workspace.exists():
            continue
        files = _find_result_files(workspace)
        for fpath in files:
            try:
                stat = fpath.stat()
                items.append({
                    "task_id": task.task_id,
                    "task_type": task.task_type,
                    "file_name": fpath.name,
                    "file_path": str(fpath.relative_to(workspace)),
                    "file_size": stat.st_size,
                    "row_count": _count_file_rows(fpath),
                    "created_at": task.completed_at or task.created_at,
                    "keywords": task.params.get("keywords", []),
                })
            except OSError:
                continue

    # 按完成时间降序
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return {"items": items, "total": len(items)}


@router.get("/data/completeness", summary="检查任务数据完整性")
async def data_completeness(
    task_id: str = Query(..., description="任务 ID"),
) -> Dict[str, Any]:
    tm = get_task_manager()
    if not tm.is_valid_task_id(task_id):
        raise HTTPException(status_code=400, detail="无效 task_id")
    task = tm.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    workspace = Path(task.workspace)
    if not workspace.exists():
        raise HTTPException(status_code=404, detail="任务 workspace 不存在")
    report = write_task_completeness_report(workspace, task_id=task_id)
    update_video_completeness_index(workspace, task_id=task_id, report=report)
    tm.update_task_data_quality(task_id, report)
    return report


@router.get("/data/preview/{task_id}", summary="预览任务结果数据")
async def preview_data(
    task_id: str,
    limit: int = Query(100, ge=1, le=1000, description="返回行数上限"),
) -> Dict[str, Any]:
    """Preview the same primary result selected by /scrape/result."""
    tm = get_task_manager()
    task = tm.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    if task.status != "completed":
        raise HTTPException(
            status_code=400,
            detail=f"任务未完成，当前状态: {task.status}",
        )

    target = tm.get_result_path(task_id)
    if not target or not target.is_file():
        raise HTTPException(status_code=404, detail="无可用预览数据")

    if target.suffix == ".csv":
        rows = _read_csv_rows(target, limit=limit)
        total_rows = max(_count_file_rows(target) - 1, 0)
        file_format = "csv"
    elif target.suffix == ".jsonl":
        rows = _read_jsonl_rows(target, limit=limit)
        total_rows = _count_file_rows(target)
        file_format = "jsonl"
    else:
        raise HTTPException(status_code=404, detail="无可用预览数据")

    return {
        "task_id": task_id,
        "file_name": target.name,
        "rows": rows,
        "format": file_format,
        "total_rows": total_rows,
    }


class ExportRequest(BaseModel):
    """数据导出请求"""
    task_ids: List[str] = Field(..., description="任务 ID 列表（支持多选）")
    format: Literal["csv", "txt"] = Field("csv", description="导出格式")
    limit: int = Field(MAX_EXPORT_ROWS, ge=1, le=MAX_EXPORT_ROWS, description="最大行数上限")

    @field_validator("task_ids")
    @classmethod
    def validate_task_ids(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("task_ids 不能为空")
        if len(v) > 50:
            raise ValueError("一次最多导出 50 个任务")
        return v


@router.get("/data/export", summary="导出任务结果文件（直接下载）")
async def data_export_download(
    task_id: str = Query(..., description="任务 ID"),
):
    """Download the same primary result selected by /scrape/result."""
    tm = get_task_manager()
    task = tm.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")
    if task.status != "completed":
        raise HTTPException(
            status_code=400,
            detail=f"任务未完成，当前状态: {task.status}",
        )

    target = tm.get_result_path(task_id)
    if not target or not target.is_file():
        raise HTTPException(status_code=404, detail="无可用导出数据")

    media_type = "application/octet-stream"
    if target.suffix == ".csv":
        media_type = "text/csv"
    elif target.suffix == ".jsonl":
        media_type = "application/jsonl"

    return FileResponse(
        path=str(target),
        media_type=media_type,
        filename=target.name,
    )


@router.post("/data/export", summary="批量导出数据（CSV 或 TXT）")
async def export_data(req: ExportRequest):
    """
    批量导出多个任务的结果数据。

    CSV 格式：列 video_id, platform, script_text, likes, favorites, shares, comments（| 分隔多值）
    TXT 格式：每行一条，字段用 || 分隔：video_id||script_text||likes||favorites||shares||comments
              上限 200 行、2MB、UTF-8
    """
    tm = get_task_manager()

    # 收集所有行
    all_rows: List[Dict[str, Any]] = []
    for task_id in req.task_ids:
        task = tm.get_task(task_id)
        if not task or task.status != "completed":
            continue
        target = tm.get_result_path(task_id)
        if not target or not target.is_file():
            continue
        if target.suffix == ".csv":
            rows = _read_csv_rows(target, limit=req.limit)
        else:
            rows = _read_jsonl_rows(target, limit=req.limit)
        all_rows.extend(rows)
        if len(all_rows) >= req.limit:
            all_rows = all_rows[:req.limit]
            break

    if not all_rows:
        raise HTTPException(status_code=404, detail="未找到可导出的数据（任务未完成或无结果文件）")

    if req.format == "csv":
        # CSV 格式：标准 CSV，comments 字段多值用 | 分隔
        output = io.StringIO()
        fieldnames = ["video_id", "platform", "script_text", "likes", "favorites", "shares", "comments"]
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        size = 0
        written = 0
        for row in all_rows:
            norm = _normalize_row(row)
            writer.writerow(norm)
            written += 1
            size = output.tell()
            if size >= MAX_EXPORT_BYTES:
                break
        content = output.getvalue().encode("utf-8-sig")
        media_type = "text/csv; charset=utf-8"
        filename = f"export_{len(req.task_ids)}tasks_{written}rows.csv"

    else:  # txt
        # TXT 格式：每行 video_id||script_text||likes||favorites||shares||comments
        lines: List[str] = []
        size = 0
        for row in all_rows:
            norm = _normalize_row(row)
            parts = [
                norm["video_id"],
                norm["script_text"],
                norm["likes"],
                norm["favorites"],
                norm["shares"],
                norm["comments"],
            ]
            line = "||".join(parts) + "\n"
            line_bytes = line.encode("utf-8")
            if size + len(line_bytes) > MAX_EXPORT_BYTES:
                break
            lines.append(line)
            size += len(line_bytes)
        content = "".join(lines).encode("utf-8")
        media_type = "text/plain; charset=utf-8"
        filename = f"export_{len(req.task_ids)}tasks_{len(lines)}rows.txt"

    from fastapi.responses import Response
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# WebSocket 路由已移至 main.py（路径：/ws/tasks），
# 避免 /scrape 前缀导致路径不匹配。
