"""读数归档 API 路由。

- POST /api/v1/archive/fragments            上传同一 UTC 日的读数行，保存为独立 NDJSON 片段
- GET  /api/v1/archive/fragments            片段数查询（可选按日过滤）
- GET  /api/v1/archive/days/{day}/download  按日下载全部原始行（NDJSON，重复行保留）
- POST /api/v1/archive/merges               合并选定片段为一个新片段
- POST /api/v1/archive/cleanup              清理废弃文件（不打断进行中的下载）
"""
from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import StreamingResponse

from .archive_models import (
    ArchiveRow,
    CleanupResponse,
    FragmentQueryResponse,
    MergeRequest,
    MergeResponse,
    UploadRequest,
    UploadResponse,
)
from .archive_store import ArchiveStore, MergeConflict

router = APIRouter(prefix="/api/v1/archive", tags=["读数归档"])

_store: ArchiveStore | None = None
_store_lock = threading.Lock()


def get_store() -> ArchiveStore:
    """惰性创建存储引擎（数据目录取环境变量 ARCHIVE_DATA_DIR，默认 ./data）。"""
    global _store
    with _store_lock:
        if _store is None:
            _store = ArchiveStore(Path(os.environ.get("ARCHIVE_DATA_DIR", "data")))
        return _store


def reset_store_for_tests() -> None:
    """关闭并丢弃当前存储引擎，下次请求按最新环境变量重建（测试用）。"""
    global _store
    with _store_lock:
        if _store is not None:
            _store.close()
            _store = None


def _row_to_line(row: ArchiveRow) -> bytes:
    payload = {
        "meter_id": row.meter_id,
        "time": row.time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "kwh": row.kwh,
    }
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# 交互式文档示例（Swagger UI /docs 中可直接 Try it out）
# ---------------------------------------------------------------------------

_EXAMPLE_UPLOAD = {
    "rows": [
        {"meter_id": "sub_a", "time": "2026-01-01T00:00:00Z", "kwh": 500.0},
        {"meter_id": "sub_a", "time": "2026-01-01T01:00:00Z", "kwh": 510.5},
        {"meter_id": "sub_b", "time": "2026-01-01T00:00:00Z", "kwh": 200.0},
        {"meter_id": "sub_b", "time": "2026-01-01T01:00:00Z", "kwh": 208.25},
    ]
}

_EXAMPLE_MERGE = {
    "day": "2026-01-01",
    "fragment_ids": [
        "2026-01-01-<先从片段数查询复制 fragment_id>",
        "2026-01-01-<再粘贴第二个 fragment_id>",
    ],
}


@router.post(
    "/fragments",
    status_code=201,
    response_model=UploadResponse,
    summary="上传读数行，保存为独立 NDJSON 片段",
    description=(
        "每次上传的读数行（表 ID、时刻、累计 kWh）必须落在同一 UTC 日；"
        "服务端先写临时文件再原子改名，并登记 SQLite 清单后才返回，"
        "进程中断也不会暴露未写完的片段。"
    ),
)
def upload_fragment(
    req: UploadRequest = Body(
        openapi_examples={
            "网关批量上传同一 UTC 日的读数": {
                "summary": "4 行读数，时刻均在 2026-01-01（UTC），保存为一个片段",
                "value": _EXAMPLE_UPLOAD,
            }
        }
    ),
) -> UploadResponse:
    days = {r.time.date() for r in req.rows}
    if len(days) != 1:
        raise HTTPException(
            status_code=422,
            detail="所有行的时刻必须落在同一 UTC 日，请拆分后分别上传",
        )
    day = days.pop()
    lines = [_row_to_line(r) for r in req.rows]
    rec = get_store().add_upload(day, lines)
    return UploadResponse(fragment_id=rec.id, day=date.fromisoformat(rec.day),
                          rows=rec.rows, bytes=rec.bytes)


@router.get(
    "/fragments",
    response_model=FragmentQueryResponse,
    summary="片段数查询（可选按 UTC 日过滤）",
    description="返回当前有效片段数、行数、字节数及片段清单；合并后被替换的片段不计入。",
)
def query_fragments(
    day: date | None = Query(
        default=None, description="UTC 日，如 2026-01-01；缺省返回所有日"
    ),
) -> FragmentQueryResponse:
    return FragmentQueryResponse(days=get_store().query_fragments(day))


@router.get(
    "/days/{day}/download",
    summary="按日下载全部原始行（NDJSON）",
    description=(
        "下载开始时固定当日的有效片段集合，逐片段拼接输出全部原始行"
        "（重复行保留）；下载期间并发的合并与清理不会打断本次下载。"
    ),
    responses={200: {"content": {"application/x-ndjson": {}},
                     "description": "NDJSON 原始行流"},
               404: {"description": "该日暂无归档片段"}},
)
def download_day(day: date) -> StreamingResponse:
    store = get_store()
    snapshot = store.snapshot_download(day)
    if not snapshot.rels:
        snapshot.release()
        raise HTTPException(status_code=404, detail=f"{day} 暂无归档片段")
    headers = {
        "Content-Disposition": f'attachment; filename="archive-{day.isoformat()}.ndjson"'
    }
    return StreamingResponse(
        snapshot.stream(), media_type="application/x-ndjson", headers=headers
    )


@router.post(
    "/merges",
    response_model=MergeResponse,
    summary="合并选定片段为一个新片段",
    description=(
        "把开始时选定的片段顺序拼接写成一个新片段，提交只替换选中片段；"
        "合并期间的上传不受影响、新片段保留。若并发合并已替换其中任一片段，"
        "本次合并整体不生效并返回 409。"
    ),
    responses={409: {"description": "选中片段不存在或已被并发合并替换"}},
)
def merge_fragments(
    req: MergeRequest = Body(
        openapi_examples={
            "合并当日的两个小文件片段": {
                "summary": "fragment_id 可从 GET /api/v1/archive/fragments 复制",
                "value": _EXAMPLE_MERGE,
            }
        }
    ),
) -> MergeResponse:
    try:
        result = get_store().merge(req.day, req.fragment_ids)
    except MergeConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    return MergeResponse(
        day=req.day,
        merged_fragment_id=result.new_fragment.id,
        replaced_fragment_ids=result.replaced_ids,
        rows=result.new_fragment.rows,
        bytes=result.new_fragment.bytes,
        active_fragment_count=result.active_count,
    )


@router.post(
    "/cleanup",
    response_model=CleanupResponse,
    summary="清理废弃文件",
    description=(
        "删除崩溃残留的临时文件、清单未引用的孤儿文件及已被合并替换的片段文件；"
        "正在进行的上传/合并/下载所涉文件会被跳过，清理不会打断下载。"
    ),
)
def cleanup() -> CleanupResponse:
    result = get_store().cleanup()
    return CleanupResponse(
        deleted_files=result.deleted_files,
        deleted_manifest_rows=result.deleted_manifest_rows,
        skipped_pinned=result.skipped_pinned,
    )
