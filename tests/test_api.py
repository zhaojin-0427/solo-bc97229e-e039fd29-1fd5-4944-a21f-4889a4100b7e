"""API 与求解器的端到端测试。"""
import copy

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

# 观测期 2026-01-01 00:00~04:00（UTC），总表逐小时读数（小时耗电 10/40/30/20），
# 两个分表仅有首尾读数（sub_a 全期 60 kWh，sub_b 全期 40 kWh），功率上限均 20 kW。
BASE_METERS = [
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


def post(meters, meter_id, start, end):
    return client.post(
        "/api/v1/infer",
        json={
            "meters": meters,
            "query": {"meter_id": meter_id, "start": start, "end": end},
        },
    )


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_determined_by_power_caps():
    """01:00~02:00 总表耗电 40 kWh，两分表上限各 20 kW -> sub_a 唯一确定为 20。"""
    r = post(BASE_METERS, "sub_a", "2026-01-01T01:00:00Z", "2026-01-01T02:00:00Z")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["min_kwh"] == pytest.approx(20.0)
    assert body["max_kwh"] == pytest.approx(20.0)
    assert body["is_determined"] is True


def test_range_when_not_determined():
    """00:00~01:00 总表耗电 10 kWh -> sub_a 可行范围 [0, 10]。"""
    r = post(BASE_METERS, "sub_a", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z")
    body = r.json()
    assert body["status"] == "ok"
    assert body["min_kwh"] == pytest.approx(0.0)
    assert body["max_kwh"] == pytest.approx(10.0)
    assert body["is_determined"] is False


def test_query_boundary_splits_reading_interval():
    """查询边界落在读数区间内部（00:30~01:30），须细分区间精确求解。

    01:00~02:00 内 sub_a 被锁定为 20 kWh，其前半小时必为 10 kWh；
    00:30~01:00 部分可行范围 [0, 10]，故查询窗口合计 [10, 20]。
    线性插值无法给出该边界。
    """
    r = post(BASE_METERS, "sub_a", "2026-01-01T00:30:00Z", "2026-01-01T01:30:00Z")
    body = r.json()
    assert body["status"] == "ok"
    assert body["min_kwh"] == pytest.approx(10.0)
    assert body["max_kwh"] == pytest.approx(20.0)


def test_full_period_equals_own_readings():
    """查询整个观测期时，结果就是该表自身首尾读数差 60 kWh。"""
    r = post(BASE_METERS, "sub_a", "2026-01-01T00:00:00Z", "2026-01-01T04:00:00Z")
    body = r.json()
    assert body["status"] == "ok"
    assert body["min_kwh"] == pytest.approx(60.0)
    assert body["max_kwh"] == pytest.approx(60.0)
    assert body["is_determined"] is True


def test_incompatible_imbalance():
    """总表 100 kWh 与分表合计 110 kWh 不平衡 -> 不相容。"""
    meters = copy.deepcopy(BASE_METERS)
    meters[2]["readings"][1]["value"] = 250.0  # sub_b 全期改为 50
    r = post(meters, "sub_a", "2026-01-01T00:00:00Z", "2026-01-01T04:00:00Z")
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "incompatible"
    assert body["min_kwh"] is None and body["max_kwh"] is None
    assert body["is_determined"] is None
    assert any("不平衡" in d for d in body["diagnostics"])


def test_incompatible_power_cap():
    """分表读数增量超过功率上限 -> 不相容，并指出功率上限原因。"""
    meters = [
        {
            "meter_id": "meter_total",
            "role": "total",
            "readings": [
                {"time": "2026-01-01T00:00:00Z", "value": 0.0},
                {"time": "2026-01-01T01:00:00Z", "value": 100.0},
            ],
        },
        {
            "meter_id": "sub_a",
            "role": "sub",
            "max_power_kw": 20.0,
            "readings": [
                {"time": "2026-01-01T00:00:00Z", "value": 0.0},
                {"time": "2026-01-01T01:00:00Z", "value": 100.0},
            ],
        },
        {
            "meter_id": "sub_b",
            "role": "sub",
            "max_power_kw": 20.0,
            "readings": [
                {"time": "2026-01-01T00:00:00Z", "value": 0.0},
                {"time": "2026-01-01T01:00:00Z", "value": 0.0},
            ],
        },
    ]
    r = post(meters, "sub_a", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z")
    body = r.json()
    assert body["status"] == "incompatible"
    assert any("功率上限" in d for d in body["diagnostics"])


def test_incompatible_decreasing_reading():
    """累计读数下降（回零）-> 不相容。"""
    meters = copy.deepcopy(BASE_METERS)
    meters[1]["readings"][1]["value"] = 400.0  # sub_a 末读数低于首读数
    meters[2]["readings"][1]["value"] = 800.0  # 保持总量平衡，隔离变量
    r = post(meters, "sub_a", "2026-01-01T00:00:00Z", "2026-01-01T04:00:00Z")
    body = r.json()
    assert body["status"] == "incompatible"
    assert any("回零" in d for d in body["diagnostics"])


def test_timezone_offset_normalized():
    """带 +08:00 偏移的时刻与 UTC 等价。"""
    meters = copy.deepcopy(BASE_METERS)
    for m in meters:
        for rd in m["readings"]:
            rd["time"] = rd["time"].replace("T00:00:00Z", "T08:00:00+08:00")
    r = post(meters, "sub_a", "2026-01-01T09:00:00+08:00", "2026-01-01T10:00:00+08:00")
    body = r.json()
    assert body["status"] == "ok"
    assert body["min_kwh"] == pytest.approx(20.0)
    assert body["max_kwh"] == pytest.approx(20.0)


def test_six_submeters_accepted():
    """6 个分表的边界情形可用。"""
    meters = [copy.deepcopy(BASE_METERS[0])]
    for i in range(6):
        meters.append(
            {
                "meter_id": f"sub_{i}",
                "role": "sub",
                "max_power_kw": 20.0,
                "readings": [
                    {"time": "2026-01-01T00:00:00Z", "value": 0.0},
                    {"time": "2026-01-01T04:00:00Z", "value": 100.0 / 6},
                ],
            }
        )
    r = post(meters, "sub_0", "2026-01-01T00:00:00Z", "2026-01-01T04:00:00Z")
    body = r.json()
    assert body["status"] == "ok"
    assert body["min_kwh"] == pytest.approx(100.0 / 6)


# ---------------------------------------------------------------------------
# 入参校验（422）
# ---------------------------------------------------------------------------


def test_reject_single_submeter():
    r = post(BASE_METERS[:2], "sub_a", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z")
    assert r.status_code == 422


def test_reject_seven_submeters():
    meters = [copy.deepcopy(BASE_METERS[0])]
    for i in range(7):
        meters.append(
            {
                "meter_id": f"sub_{i}",
                "role": "sub",
                "max_power_kw": 20.0,
                "readings": [
                    {"time": "2026-01-01T00:00:00Z", "value": 0.0},
                    {"time": "2026-01-01T04:00:00Z", "value": 10.0},
                ],
            }
        )
    r = post(meters, "sub_0", "2026-01-01T00:00:00Z", "2026-01-01T04:00:00Z")
    assert r.status_code == 422


def test_reject_query_total_meter():
    r = post(
        BASE_METERS, "meter_total", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z"
    )
    assert r.status_code == 422


def test_reject_missing_endpoint_reading():
    meters = copy.deepcopy(BASE_METERS)
    meters[2]["readings"][1] = {"time": "2026-01-01T03:00:00Z", "value": 230.0}
    r = post(meters, "sub_a", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z")
    assert r.status_code == 422
    assert "首尾" in r.text


def test_reject_naive_datetime():
    meters = copy.deepcopy(BASE_METERS)
    meters[0]["readings"][0]["time"] = "2026-01-01T00:00:00"
    r = post(meters, "sub_a", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z")
    assert r.status_code == 422


def test_reject_query_outside_observation():
    r = post(BASE_METERS, "sub_a", "2026-01-01T00:00:00Z", "2026-01-01T05:00:00Z")
    assert r.status_code == 422


def test_reject_reversed_query_window():
    r = post(BASE_METERS, "sub_a", "2026-01-01T02:00:00Z", "2026-01-01T01:00:00Z")
    assert r.status_code == 422


def test_reject_submeter_without_power_cap():
    meters = copy.deepcopy(BASE_METERS)
    del meters[1]["max_power_kw"]
    r = post(meters, "sub_a", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z")
    assert r.status_code == 422
