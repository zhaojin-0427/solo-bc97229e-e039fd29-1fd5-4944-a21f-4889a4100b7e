"""读数归档 API 的请求/响应模型。"""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator

from .models import _require_tz


class ArchiveRow(BaseModel):
    """一行原始读数：表 ID、时刻、累计 kWh。"""

    meter_id: str = Field(min_length=1, max_length=128, description="表计标识")
    time: datetime = Field(description="读数时刻（须带时区，统一按 UTC 归一）")
    kwh: float = Field(ge=0, description="累计电量读数（kWh）")

    _normalize_time = field_validator("time")(_require_tz)


class UploadRequest(BaseModel):
    """上传请求：同一 UTC 日的若干读数行，保存为一个独立 NDJSON 片段。"""

    rows: list[ArchiveRow] = Field(
        min_length=1,
        description="读数行，所有行的时刻（按 UTC）必须落在同一日",
    )


class UploadResponse(BaseModel):
    fragment_id: str = Field(description="新片段 ID")
    day: date = Field(description="片段所属 UTC 日")
    rows: int = Field(description="片段内行数")
    bytes: int = Field(description="片段字节数")


class FragmentInfo(BaseModel):
    fragment_id: str
    rows: int
    bytes: int
    created_at: str


class DayFragmentStats(BaseModel):
    day: date
    fragment_count: int = Field(description="该日当前有效片段数")
    total_rows: int
    total_bytes: int
    fragments: list[FragmentInfo]


class FragmentQueryResponse(BaseModel):
    days: list[DayFragmentStats]


class MergeRequest(BaseModel):
    """合并请求：把开始时选定的片段合并写成一个新片段。"""

    day: date = Field(description="UTC 日，如 2026-01-01")
    fragment_ids: list[str] = Field(
        min_length=1, description="待合并的片段 ID（须全部为该日的有效片段）"
    )


class MergeResponse(BaseModel):
    day: date
    merged_fragment_id: str = Field(description="合并生成的新片段 ID")
    replaced_fragment_ids: list[str] = Field(description="被替换（不再有效）的片段 ID")
    rows: int = Field(description="新片段行数（选中片段行数之和，重复行保留）")
    bytes: int
    active_fragment_count: int = Field(description="合并后该日的有效片段数")


class CleanupResponse(BaseModel):
    deleted_files: int = Field(description="本次删除的废弃文件数")
    deleted_manifest_rows: int = Field(description="本次删除的已替换清单行数")
    skipped_pinned: int = Field(
        description="因正在进行上传/合并/下载而跳过、留待下次清理的文件数"
    )
