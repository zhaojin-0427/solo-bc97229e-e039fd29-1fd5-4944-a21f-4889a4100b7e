"""读数归档 API 端到端与崩溃恢复测试。

覆盖：上传/下载（重复行保留）、UTC 日归一与校验、片段数查询、
合并语义（期间可上传、只替换选中片段、同组并发合并最多一个生效）、
下载不被清理打断、进程中断后的重启恢复（成功上传的行各保留一次、
不暴露未写完文件、可继续清理废弃文件）。
"""
import json
import threading
from collections import Counter
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import archive_api
from app.archive_store import ArchiveStore, MergeConflict
from app.main import app

DAY = "2026-01-01"
DAY2 = "2026-01-02"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个用例使用独立数据目录的 TestClient。"""
    monkeypatch.setenv("ARCHIVE_DATA_DIR", str(tmp_path / "data"))
    archive_api.reset_store_for_tests()
    with TestClient(app) as c:
        yield c
    archive_api.reset_store_for_tests()


def upload(client, rows, day_rows=None):
    return client.post("/api/v1/archive/fragments", json={"rows": rows})


def row(meter="sub_a", time=f"{DAY}T00:00:00Z", kwh=100.0):
    return {"meter_id": meter, "time": time, "kwh": kwh}


def download_rows(client, day=DAY):
    r = client.get(f"/api/v1/archive/days/{day}/download")
    assert r.status_code == 200
    return [json.loads(line) for line in r.text.splitlines() if line.strip()]


def fragment_ids(client, day=DAY):
    r = client.get("/api/v1/archive/fragments", params={"day": day})
    assert r.status_code == 200
    days = r.json()["days"]
    if not days:
        return []
    return [f["fragment_id"] for f in days[0]["fragments"]]


# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------


def test_upload_creates_fragment(client):
    r = upload(client, [row(kwh=1.0), row(time=f"{DAY}T01:00:00Z", kwh=2.5)])
    assert r.status_code == 201
    body = r.json()
    assert body["day"] == DAY
    assert body["rows"] == 2
    assert body["bytes"] > 0
    assert body["fragment_id"]


def test_upload_normalizes_timezone_to_utc_day(client):
    """+08:00 的 2026-01-02T07:30 即 UTC 2026-01-01T23:30，归入 2026-01-01。"""
    r = upload(client, [row(time="2026-01-02T07:30:00+08:00")])
    assert r.status_code == 201
    assert r.json()["day"] == DAY
    (line,) = download_rows(client)
    assert line["time"] == "2026-01-01T23:30:00Z"


def test_upload_rejects_mixed_utc_days(client):
    r = upload(client, [row(), row(time=f"{DAY2}T00:00:00Z")])
    assert r.status_code == 422
    assert "同一 UTC 日" in r.text


def test_upload_rejects_empty_rows(client):
    assert upload(client, []).status_code == 422


def test_upload_rejects_naive_datetime(client):
    r = upload(client, [row(time="2026-01-01T00:00:00")])
    assert r.status_code == 422


def test_upload_rejects_negative_kwh(client):
    r = upload(client, [row(kwh=-1.0)])
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 下载与片段数查询
# ---------------------------------------------------------------------------


def test_download_preserves_all_rows_including_duplicates(client):
    """同一批行上传两次（网关重发），下载须原样保留重复行。"""
    batch = [row(kwh=1.0), row(time=f"{DAY}T01:00:00Z", kwh=2.0)]
    assert upload(client, batch).status_code == 201
    assert upload(client, batch).status_code == 201
    lines = download_rows(client)
    assert len(lines) == 4
    assert Counter(json.dumps(x, sort_keys=True) for x in lines) == Counter(
        json.dumps(x, sort_keys=True)
        for x in [
            {"meter_id": "sub_a", "time": "2026-01-01T00:00:00Z", "kwh": 1.0},
            {"meter_id": "sub_a", "time": "2026-01-01T01:00:00Z", "kwh": 2.0},
            {"meter_id": "sub_a", "time": "2026-01-01T00:00:00Z", "kwh": 1.0},
            {"meter_id": "sub_a", "time": "2026-01-01T01:00:00Z", "kwh": 2.0},
        ]
    )


def test_download_unknown_day_returns_404(client):
    r = client.get(f"/api/v1/archive/days/{DAY}/download")
    assert r.status_code == 404


def test_download_headers(client):
    upload(client, [row()])
    r = client.get(f"/api/v1/archive/days/{DAY}/download")
    assert r.headers["content-type"].startswith("application/x-ndjson")
    assert f'filename="archive-{DAY}.ndjson"' in r.headers["content-disposition"]


def test_fragment_count_query(client):
    upload(client, [row(kwh=1.0)])
    upload(client, [row(kwh=2.0), row(time=f"{DAY}T01:00:00Z", kwh=3.0)])
    upload(client, [row(time=f"{DAY2}T00:00:00Z", kwh=9.0)])

    r = client.get("/api/v1/archive/fragments", params={"day": DAY})
    (stats,) = r.json()["days"]
    assert stats["day"] == DAY
    assert stats["fragment_count"] == 2
    assert stats["total_rows"] == 3
    assert len(stats["fragments"]) == 2

    r_all = client.get("/api/v1/archive/fragments")
    assert {d["day"]: d["fragment_count"] for d in r_all.json()["days"]} == {
        DAY: 2,
        DAY2: 1,
    }

    r_empty = client.get("/api/v1/archive/fragments", params={"day": "2026-02-01"})
    assert r_empty.json()["days"] == []


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------


def test_merge_replaces_selected_and_keeps_new_uploads(client):
    """合并只替换选中片段；合并期间（之前）上传的未选中片段保留。"""
    ids = [upload(client, [row(kwh=i)]).json()["fragment_id"] for i in (1, 2, 3)]
    r = client.post(
        "/api/v1/archive/merges", json={"day": DAY, "fragment_ids": ids[:2]}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["rows"] == 2
    assert body["active_fragment_count"] == 2
    assert sorted(body["replaced_fragment_ids"]) == sorted(ids[:2])

    stats = client.get("/api/v1/archive/fragments", params={"day": DAY}).json()
    (day_stats,) = stats["days"]
    assert day_stats["fragment_count"] == 2
    active_ids = {f["fragment_id"] for f in day_stats["fragments"]}
    assert active_ids == {body["merged_fragment_id"], ids[2]}

    lines = download_rows(client)
    assert sorted(x["kwh"] for x in lines) == [1.0, 2.0, 3.0]


def test_merge_merged_content_concatenates_selected(client):
    ids = [
        upload(client, [row(kwh=1.0), row(time=f"{DAY}T01:00:00Z", kwh=2.0)]).json()[
            "fragment_id"
        ],
        upload(client, [row(kwh=1.0)]).json()["fragment_id"],  # 与上行重复，须保留
    ]
    r = client.post("/api/v1/archive/merges", json={"day": DAY, "fragment_ids": ids})
    assert r.status_code == 200
    assert r.json()["rows"] == 3
    lines = download_rows(client)
    assert [x["kwh"] for x in lines] == [1.0, 2.0, 1.0]


def test_merge_same_set_twice_second_fails(client):
    """同组选中片段的合并最多一个生效：第二次同样选择返回 409。"""
    ids = [upload(client, [row(kwh=i)]).json()["fragment_id"] for i in (1, 2)]
    r1 = client.post("/api/v1/archive/merges", json={"day": DAY, "fragment_ids": ids})
    assert r1.status_code == 200
    r2 = client.post("/api/v1/archive/merges", json={"day": DAY, "fragment_ids": ids})
    assert r2.status_code == 409


def test_merge_unknown_fragment_returns_409(client):
    upload(client, [row()])
    r = client.post(
        "/api/v1/archive/merges",
        json={"day": DAY, "fragment_ids": ["no-such-fragment"]},
    )
    assert r.status_code == 409


def test_merge_rejects_empty_selection(client):
    r = client.post("/api/v1/archive/merges", json={"day": DAY, "fragment_ids": []})
    assert r.status_code == 422


def test_merge_wrong_day_returns_409(client):
    fid = upload(client, [row()]).json()["fragment_id"]
    r = client.post(
        "/api/v1/archive/merges", json={"day": DAY2, "fragment_ids": [fid]}
    )
    assert r.status_code == 409


def test_concurrent_merge_same_set_only_one_succeeds(tmp_path):
    """两个线程同时合并同一组片段：恰好一个生效，数据不丢不重。"""
    store = ArchiveStore(tmp_path / "data")
    try:
        ids = [
            store.add_upload(date(2026, 1, 1), [f'{{"i": {i}}}\n'.encode()]).id
            for i in range(4)
        ]
        barrier = threading.Barrier(2)

        def hook():
            barrier.wait(timeout=10)  # 两个合并都通过选定检查后才放行提交

        store._before_merge_commit_hook = hook
        results, errors = [], []

        def do_merge():
            try:
                results.append(store.merge(date(2026, 1, 1), ids))
            except MergeConflict as e:
                errors.append(e)

        threads = [threading.Thread(target=do_merge) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(results) == 1 and len(errors) == 1
        # 清单中只剩合并后的一个片段，内容为 4 个上传各一次
        stats = store.query_fragments(date(2026, 1, 1))
        assert stats[0]["fragment_count"] == 1
        assert stats[0]["total_rows"] == 4
        snap = store.snapshot_download(date(2026, 1, 1))
        data = b"".join(snap.stream())
        assert sorted(json.loads(line)["i"] for line in data.splitlines()) == [0, 1, 2, 3]
    finally:
        store.close()


def test_upload_during_merge_is_kept(tmp_path):
    """合并提交前并发上传的新片段不被替换，合并后仍有效。"""
    store = ArchiveStore(tmp_path / "data")
    try:
        day = date(2026, 1, 1)
        ids = [
            store.add_upload(day, [f'{{"i": {i}}}\n'.encode()]).id for i in range(2)
        ]
        entered = threading.Event()
        release = threading.Event()

        def hook():
            entered.set()
            assert release.wait(timeout=10)

        store._before_merge_commit_hook = hook
        outcome = {}

        def do_merge():
            try:
                outcome["result"] = store.merge(day, ids)
            except MergeConflict as e:
                outcome["error"] = e

        t = threading.Thread(target=do_merge)
        t.start()
        assert entered.wait(timeout=10)
        new_id = store.add_upload(day, [b'{"i": 99}\n']).id  # 合并期间上传
        release.set()
        t.join(timeout=30)

        assert "result" in outcome
        stats = store.query_fragments(day)[0]
        active_ids = {f["fragment_id"] for f in stats["fragments"]}
        assert active_ids == {outcome["result"].new_fragment.id, new_id}
        snap = store.snapshot_download(day)
        data = b"".join(snap.stream())
        assert sorted(json.loads(line)["i"] for line in data.splitlines()) == [0, 1, 99]
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 清理与下载的相互关系
# ---------------------------------------------------------------------------


def test_cleanup_removes_replaced_files_and_rows(client, tmp_path):
    ids = [upload(client, [row(kwh=i)]).json()["fragment_id"] for i in (1, 2)]
    client.post("/api/v1/archive/merges", json={"day": DAY, "fragment_ids": ids})
    data_dir = tmp_path / "data"
    # 合并后被替换的片段文件仍在，等待清理
    assert len(list((data_dir / "fragments").iterdir())) == 3

    r = client.post("/api/v1/archive/cleanup")
    assert r.status_code == 200
    body = r.json()
    assert body["deleted_files"] == 2
    assert body["deleted_manifest_rows"] == 2
    assert len(list((data_dir / "fragments").iterdir())) == 1
    # 数据不受影响
    assert sorted(x["kwh"] for x in download_rows(client)) == [1.0, 2.0]


def test_cleanup_does_not_interrupt_download(tmp_path):
    """下载固定开始时的片段集合；清理旧文件不得打断下载。"""
    store = ArchiveStore(tmp_path / "data")
    try:
        day = date(2026, 1, 1)
        ids = [
            store.add_upload(day, [f'{{"i": {i}}}\n'.encode()]).id for i in range(2)
        ]
        snapshot = store.snapshot_download(day)  # 下载开始，固定片段集合
        store.merge(day, ids)  # 合并替换选中片段
        result = store.cleanup()  # 清理不得删除下载中的文件
        assert result.skipped_pinned == 2
        assert result.deleted_files == 0
        data = b"".join(snapshot.stream())  # 下载照常完成
        assert sorted(json.loads(line)["i"] for line in data.splitlines()) == [0, 1]
        # 下载结束后清理可继续回收
        result2 = store.cleanup()
        assert result2.deleted_files == 2
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 崩溃恢复
# ---------------------------------------------------------------------------


def test_restart_recovers_and_keeps_every_successful_upload(tmp_path, monkeypatch):
    """进程中断后重启：每次成功上传的全部行仍各保留一次，废弃文件可继续清理。"""
    monkeypatch.setenv("ARCHIVE_DATA_DIR", str(tmp_path / "data"))
    archive_api.reset_store_for_tests()
    with TestClient(app) as c:
        ids = [upload(c, [row(kwh=i)]).json()["fragment_id"] for i in (1, 2)]
        upload(c, [row(kwh=3.0)])
        # 合并已提交，但清理尚未执行时“进程中断”
        c.post("/api/v1/archive/merges", json={"day": DAY, "fragment_ids": ids})
    data_dir = tmp_path / "data"
    # 模拟崩溃残留：写入未完成的临时文件 + 改名后未登记清单的孤儿文件
    (data_dir / "tmp" / "partial.tmp").write_bytes(b'{"meter_id": "x", "kwh": 1')
    (data_dir / "fragments" / "orphan.ndjson").write_bytes(b'{"kwh": 666}\n')
    n_before = len(list((data_dir / "fragments").iterdir()))

    # 重启（新进程重建存储引擎，启动即恢复）
    archive_api.reset_store_for_tests()
    with TestClient(app) as c2:
        lines = download_rows(c2)
        # 3 次成功上传的行各保留一次（1、2 已合并，3 为独立片段）
        assert sorted(x["kwh"] for x in lines) == [1.0, 2.0, 3.0]
        # 未写完/未登记的文件不暴露且已被清理
        assert n_before == 5  # 合并产物 + 被替换的 2 个 + 第 3 次上传 + 孤儿
        remaining = list((data_dir / "fragments").iterdir())
        assert len(remaining) == 2  # 合并产物 + 第 3 次上传
        assert list((data_dir / "tmp").iterdir()) == []
        stats = c2.get("/api/v1/archive/fragments", params={"day": DAY}).json()
        assert stats["days"][0]["fragment_count"] == 2
    archive_api.reset_store_for_tests()


def test_restart_after_crash_before_manifest_commit(tmp_path):
    """文件已改名但清单未提交即崩溃：重启后该文件被回收，不出现在下载中。"""
    store = ArchiveStore(tmp_path / "data")
    day = date(2026, 1, 1)
    store.add_upload(day, [b'{"kwh": 1}\n'])
    # 模拟“上传写文件后、登记清单前崩溃”留下的孤儿文件
    (tmp_path / "data" / "fragments" / "half-committed.ndjson").write_bytes(
        b'{"kwh": 2}\n'
    )
    store.close()

    store2 = ArchiveStore(tmp_path / "data")  # 重启恢复
    try:
        snap = store2.snapshot_download(day)
        data = b"".join(snap.stream())
        assert [json.loads(line)["kwh"] for line in data.splitlines()] == [1]
        assert not (tmp_path / "data" / "fragments" / "half-committed.ndjson").exists()
    finally:
        store2.close()


def test_orphan_rows_never_exposed_in_download(client, tmp_path):
    """清单外的文件即使内容完整，也不会出现在下载结果里。"""
    upload(client, [row(kwh=1.0)])
    (tmp_path / "data" / "fragments" / "sneaky.ndjson").write_bytes(b'{"kwh": 9}\n')
    lines = download_rows(client)
    assert [x["kwh"] for x in lines] == [1.0]


# ---------------------------------------------------------------------------
# 原推算接口保持可用
# ---------------------------------------------------------------------------


def test_infer_api_still_works(client):
    r = client.get("/health")
    assert r.status_code == 200
    r = client.post(
        "/api/v1/infer",
        json={
            "meters": [
                {
                    "meter_id": "meter_total",
                    "role": "total",
                    "readings": [
                        {"time": "2026-01-01T00:00:00Z", "value": 0.0},
                        {"time": "2026-01-01T01:00:00Z", "value": 40.0},
                    ],
                },
                {
                    "meter_id": "sub_a",
                    "role": "sub",
                    "max_power_kw": 20.0,
                    "readings": [
                        {"time": "2026-01-01T00:00:00Z", "value": 0.0},
                        {"time": "2026-01-01T01:00:00Z", "value": 20.0},
                    ],
                },
                {
                    "meter_id": "sub_b",
                    "role": "sub",
                    "max_power_kw": 20.0,
                    "readings": [
                        {"time": "2026-01-01T00:00:00Z", "value": 0.0},
                        {"time": "2026-01-01T01:00:00Z", "value": 20.0},
                    ],
                },
            ],
            "query": {
                "meter_id": "sub_a",
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-01T01:00:00Z",
            },
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["min_kwh"] == pytest.approx(20.0)
