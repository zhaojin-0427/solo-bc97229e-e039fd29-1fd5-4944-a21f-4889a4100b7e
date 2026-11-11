"""园区分表电量推算 API。

结合总表与各分表的累计读数、分表功率上限，用线性规划求指定分表在
指定时段内可行耗电量的精确最小/最大值，用于判断结算分界时刻缺测的
分表电量是否足以确定、是否需要补抄。
"""
from __future__ import annotations

from fastapi import Body, FastAPI

from .models import InferRequest, InferResponse
from .solver import MeterSeries, solve_window

app = FastAPI(
    title="园区分表电量推算 API",
    description=(
        "输入采集系统导出的 1 个总表与 2~6 个分表的累计电量读数（UTC）"
        "及各分表功率上限，基于线性规划求指定分表在指定起止时刻之间"
        "可行耗电量的最小值与最大值（精确边界，非线性插值）。"
        "约束：任意时段总表耗电 = 全部分表之和；分表耗电非负且不超功率上限；"
        "不考虑损耗、换表与回零。读数不相容时返回 status=incompatible。"
    ),
    version="1.0.0",
)

# ---------------------------------------------------------------------------
# 交互式文档示例（Swagger UI /docs 中可直接 Try it out 演示）
# ---------------------------------------------------------------------------

# 场景：观测期 2026-01-01 00:00~04:00（UTC），总表逐小时有读数，
# 两个分表只有观测期首尾读数（中间缺测），功率上限均 20 kW。
# 总表逐小时耗电：10 / 40 / 30 / 20 kWh，合计 100 kWh。
# 分表 sub_a 全期 60 kWh，sub_b 全期 40 kWh。
_EXAMPLE_METERS = {
    "meters": [
        {
            "meter_id": "meter_total",
            "role": "total",
            "readings": [
                {"time": "2026-01-01T00:00:00Z", "value": 1000.0},
                {"time": "2026-01-01T01:00:00Z", "value": 1010.0},
                {"time": "2026-01-01T02:00:00Z", "value": 1050.0},
                {"time": "2026-01-01T03:00:00Z", "value": 1080.0},
                {"time": "2026-01-01T04:00:00Z", "value": 1100.0},
            ],
        },
        {
            "meter_id": "sub_a",
            "role": "sub",
            "max_power_kw": 20.0,
            "readings": [
                {"time": "2026-01-01T00:00:00Z", "value": 500.0},
                {"time": "2026-01-01T04:00:00Z", "value": 560.0},
            ],
        },
        {
            "meter_id": "sub_b",
            "role": "sub",
            "max_power_kw": 20.0,
            "readings": [
                {"time": "2026-01-01T00:00:00Z", "value": 200.0},
                {"time": "2026-01-01T04:00:00Z", "value": 240.0},
            ],
        },
    ]
}

EXAMPLE_DETERMINED = {
    **_EXAMPLE_METERS,
    "query": {
        "meter_id": "sub_a",
        "start": "2026-01-01T01:00:00Z",
        "end": "2026-01-01T02:00:00Z",
    },
}

EXAMPLE_RANGE = {
    **_EXAMPLE_METERS,
    "query": {
        "meter_id": "sub_a",
        "start": "2026-01-01T00:00:00Z",
        "end": "2026-01-01T01:00:00Z",
    },
}

EXAMPLE_INCOMPATIBLE = {
    "meters": [
        {
            "meter_id": "meter_total",
            "role": "total",
            "readings": [
                {"time": "2026-01-01T00:00:00Z", "value": 0.0},
                {"time": "2026-01-01T04:00:00Z", "value": 100.0},
            ],
        },
        {
            "meter_id": "sub_a",
            "role": "sub",
            "max_power_kw": 20.0,
            "readings": [
                {"time": "2026-01-01T00:00:00Z", "value": 0.0},
                {"time": "2026-01-01T04:00:00Z", "value": 60.0},
            ],
        },
        {
            "meter_id": "sub_b",
            "role": "sub",
            "max_power_kw": 20.0,
            "readings": [
                {"time": "2026-01-01T00:00:00Z", "value": 0.0},
                {"time": "2026-01-01T04:00:00Z", "value": 50.0},
            ],
        },
    ],
    "query": {
        "meter_id": "sub_a",
        "start": "2026-01-01T00:00:00Z",
        "end": "2026-01-01T04:00:00Z",
    },
}


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {"service": "园区分表电量推算 API", "docs": "/docs", "health": "/health"}


@app.get("/health", include_in_schema=False)
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/api/v1/infer",
    response_model=InferResponse,
    summary="推算指定分表在指定时段的可行电量区间",
)
def infer(
    req: InferRequest = Body(
        openapi_examples={
            "电量可被唯一确定（无需补抄）": {
                "summary": "总表逐小时有读数，01:00~02:00 总表耗电 40 kWh，"
                "两个分表功率上限均 20 kW，sub_a 该时段被唯一确定为 20 kWh",
                "value": EXAMPLE_DETERMINED,
            },
            "电量只能落在区间内（需判断是否补抄）": {
                "summary": "同一组读数，查询 00:00~01:00，"
                "sub_a 该时段可行范围为 [0, 10] kWh",
                "value": EXAMPLE_RANGE,
            },
            "读数不相容": {
                "summary": "总表全期 100 kWh，但两个分表合计 110 kWh，"
                "约束无可行解",
                "value": EXAMPLE_INCOMPATIBLE,
            },
        }
    ),
) -> InferResponse:
    meters = [
        MeterSeries(
            meter_id=m.meter_id,
            role=m.role,
            max_power_kw=m.max_power_kw,
            times_ms=[_to_ms(r.time) for r in sorted(m.readings, key=lambda r: r.time)],
            values=[r.value for r in sorted(m.readings, key=lambda r: r.time)],
        )
        for m in req.meters
    ]
    result = solve_window(
        meters,
        query_meter_id=req.query.meter_id,
        q_start_ms=_to_ms(req.query.start),
        q_end_ms=_to_ms(req.query.end),
    )

    if not result.feasible:
        return InferResponse(
            status="incompatible",
            meter_id=req.query.meter_id,
            start=req.query.start,
            end=req.query.end,
            min_kwh=None,
            max_kwh=None,
            is_determined=None,
            message="读数不相容：现有读数在“总表=分表之和、分表耗电非负且受功率上限约束”下无可行解",
            diagnostics=result.diagnostics
            or ["约束联立后无可行解（未定位到单一原因，请核查各表读数）"],
        )

    is_determined = (result.max_kwh - result.min_kwh) <= 1e-6 * max(
        1.0, abs(result.max_kwh)
    )
    if is_determined:
        message = "该时段电量可被现有读数唯一确定，可直接结算，无需补抄"
    else:
        message = "该时段电量只能确定到可行区间，请结合结算精度要求判断是否需要补抄"
    return InferResponse(
        status="ok",
        meter_id=req.query.meter_id,
        start=req.query.start,
        end=req.query.end,
        min_kwh=result.min_kwh,
        max_kwh=result.max_kwh,
        is_determined=is_determined,
        message=message,
        diagnostics=[],
    )


def _to_ms(t) -> int:
    return round(t.timestamp() * 1000)
