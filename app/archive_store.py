"""读数归档存储引擎：本地文件存 NDJSON 片段，SQLite 存清单。

目录布局（data_dir 由环境变量 ARCHIVE_DATA_DIR 指定，默认 ./data）：

    data_dir/
      manifest.sqlite3   # 片段清单（id、UTC 日、文件名、状态、行数、字节数）
      fragments/         # 已提交的片段文件 <fragment_id>.ndjson
      tmp/               # 写入中的临时文件，提交前对读者不可见

崩溃安全设计（写文件、切换清单、清理任一环节进程中断都可恢复）：

1. 片段写入：先写 tmp/ 临时文件，fsync 后 os.replace 原子改名到 fragments/，
   再向 SQLite 插入 active 清单行。清单引用的文件必然完整；rename 与清单
   提交之间崩溃只留下清单未引用的孤儿文件，由清理回收，绝不暴露未写完文件。
2. 合并提交：在单个 SQLite 事务里把选中片段置为 replaced 并插入新片段
   （条件 UPDATE 校验选中片段仍全部 active，不满足则整体回滚）。事务提交前
   崩溃清单不变；提交后崩溃只遗留待清理的被替换文件。上传与合并的先后顺序
   不影响数据：每次成功上传的行都完整保留（重复行也保留）。
3. 清理：只删除 tmp 残留、清单未引用的孤儿文件、已 replaced 的片段文件。
   进行中的上传/合并/下载通过进程内 pin 注册表豁免清理，因此清理不会打断
   下载；进程重启后无 pin，启动时执行一次清理即可继续回收废弃文件。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fragments (
    id          TEXT PRIMARY KEY,
    day         TEXT NOT NULL,          -- UTC 日，YYYY-MM-DD
    filename    TEXT NOT NULL,          -- 相对 data_dir 的路径
    state       TEXT NOT NULL CHECK (state IN ('active', 'replaced')),
    rows        INTEGER NOT NULL,
    bytes       INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    replaced_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_fragments_day_state ON fragments(day, state);
"""

_CHUNK = 1024 * 1024


class MergeConflict(Exception):
    """选中片段已不全部有效（不存在或已被并发合并替换），本次合并未生效。"""


@dataclass
class FragmentRecord:
    id: str
    day: str
    filename: str
    rows: int
    bytes: int
    created_at: str


@dataclass
class MergeResult:
    new_fragment: FragmentRecord
    replaced_ids: list[str]
    active_count: int


@dataclass
class CleanupResult:
    deleted_files: int
    deleted_manifest_rows: int
    skipped_pinned: int


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class DownloadSnapshot:
    """下载开始时固定的片段集合；文件已 pin，清理不会删除，流结束后释放。"""

    def __init__(self, store: "ArchiveStore", rels: list[str]):
        self._store = store
        self.rels = rels
        self._released = False

    def stream(self, chunk_size: int = _CHUNK) -> Iterator[bytes]:
        try:
            for rel in self.rels:
                with open(self._store.data_dir / rel, "rb") as f:
                    while True:
                        chunk = f.read(chunk_size)
                        if not chunk:
                            break
                        yield chunk
        finally:
            self.release()

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._store._unpin_many(self.rels)


class ArchiveStore:
    """单进程内线程安全的归档存储（清单提交经 RLock 串行化）。"""

    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir)
        self.fragments_dir = self.data_dir / "fragments"
        self.tmp_dir = self.data_dir / "tmp"
        self.fragments_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._pins: dict[str, int] = {}
        self._db = sqlite3.connect(
            str(self.data_dir / "manifest.sqlite3"),
            check_same_thread=False,
            isolation_level=None,  # 显式管理事务
        )
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.executescript(_SCHEMA)
        # 启动恢复：回收上次进程中断留下的废弃文件（tmp 残留、孤儿文件、
        # 已被替换的片段），此刻无任何 pin，清理是安全的。
        self.cleanup()

    # ------------------------------------------------------------------
    # pin 注册表：进行中的上传/合并/下载所涉文件豁免清理
    # ------------------------------------------------------------------

    def _pin_many(self, rels: list[str]) -> None:
        with self._lock:
            for rel in rels:
                self._pins[rel] = self._pins.get(rel, 0) + 1

    def _unpin_many(self, rels: list[str]) -> None:
        with self._lock:
            for rel in rels:
                n = self._pins.get(rel, 0) - 1
                if n <= 0:
                    self._pins.pop(rel, None)
                else:
                    self._pins[rel] = n

    @contextmanager
    def _pinned(self, *rels: str):
        self._pin_many(list(rels))
        try:
            yield
        finally:
            self._unpin_many(list(rels))

    # ------------------------------------------------------------------
    # 文件写入（临时文件 + fsync + 原子改名）
    # ------------------------------------------------------------------

    def _fsync_dir(self, path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass  # 某些文件系统不支持目录 fsync，尽力而为

    def _write_atomic(self, tmp_rel: str, final_rel: str, data: bytes) -> None:
        tmp_abs = self.data_dir / tmp_rel
        final_abs = self.data_dir / final_rel
        with open(tmp_abs, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_abs, final_abs)
        self._fsync_dir(self.fragments_dir)

    # ------------------------------------------------------------------
    # 上传：写片段文件 -> 提交清单
    # ------------------------------------------------------------------

    def add_upload(self, day: date, lines: list[bytes]) -> FragmentRecord:
        """把一批 NDJSON 行保存为独立片段并登记清单（行须以 \\n 结尾）。"""
        day_s = day.isoformat()
        fid = f"{day_s}-{uuid.uuid4().hex}"
        rel = f"fragments/{fid}.ndjson"
        tmp_rel = f"tmp/{fid}.tmp"
        data = b"".join(lines)
        created = _utcnow()
        # pin 住临时与最终路径：清理不会在改名后、清单提交前误删文件
        with self._pinned(tmp_rel, rel):
            self._write_atomic(tmp_rel, rel, data)
            with self._lock:
                self._db.execute(
                    "INSERT INTO fragments (id, day, filename, state, rows, bytes, created_at)"
                    " VALUES (?, ?, ?, 'active', ?, ?, ?)",
                    (fid, day_s, rel, len(lines), len(data), created),
                )
        return FragmentRecord(fid, day_s, rel, len(lines), len(data), created)

    # ------------------------------------------------------------------
    # 片段数查询
    # ------------------------------------------------------------------

    def query_fragments(self, day: date | None = None) -> list[dict]:
        """按 UTC 日汇总 active 片段数；day 为 None 时返回所有日。"""
        with self._lock:
            if day is not None:
                rows = self._db.execute(
                    "SELECT * FROM fragments WHERE day = ? AND state = 'active'"
                    " ORDER BY rowid",
                    (day.isoformat(),),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM fragments WHERE state = 'active'"
                    " ORDER BY day, rowid"
                ).fetchall()
        days: dict[str, dict] = {}
        for r in rows:
            d = days.setdefault(
                r["day"],
                {"day": r["day"], "fragment_count": 0, "total_rows": 0,
                 "total_bytes": 0, "fragments": []},
            )
            d["fragment_count"] += 1
            d["total_rows"] += r["rows"]
            d["total_bytes"] += r["bytes"]
            d["fragments"].append(
                {
                    "fragment_id": r["id"],
                    "rows": r["rows"],
                    "bytes": r["bytes"],
                    "created_at": r["created_at"],
                }
            )
        return list(days.values())

    # ------------------------------------------------------------------
    # 下载：固定开始时的片段集合
    # ------------------------------------------------------------------

    def snapshot_download(self, day: date) -> DownloadSnapshot:
        """固定该日当前 active 片段集合并 pin 住文件，清理不会打断下载。"""
        with self._lock:
            rows = self._db.execute(
                "SELECT filename FROM fragments WHERE day = ? AND state = 'active'"
                " ORDER BY rowid",
                (day.isoformat(),),
            ).fetchall()
            rels = [r["filename"] for r in rows]
            self._pin_many(rels)
        return DownloadSnapshot(self, rels)

    # ------------------------------------------------------------------
    # 合并：选中片段 -> 一个新片段；清单原子切换
    # ------------------------------------------------------------------

    def merge(self, day: date, fragment_ids: list[str]) -> MergeResult:
        """把选中的 active 片段合并写成一个新片段。

        期间允许上传（新片段不受影响）；提交只替换选中片段。若并发合并
        已替换其中任一片段，则本次合并整体不生效并抛 MergeConflict。
        """
        ids = list(dict.fromkeys(fragment_ids))  # 去重保序
        if not ids:
            raise MergeConflict("未选择任何片段")
        day_s = day.isoformat()

        # 1) 开始时选定片段：必须全部仍是该日的 active 片段
        with self._lock:
            rows = self._db.execute(
                "SELECT rowid AS rid, * FROM fragments WHERE day = ? AND state = 'active'"
                " ORDER BY rid",
                (day_s,),
            ).fetchall()
            by_id = {r["id"]: r for r in rows}
            missing = [i for i in ids if i not in by_id]
            if missing:
                raise MergeConflict(
                    "片段不存在或已被合并：" + ", ".join(missing)
                )
            selected = sorted((by_id[i] for i in ids), key=lambda r: r["rid"])

        # 2) 读选中片段，顺序拼接写成新片段（重复行原样保留）
        new_id = f"{day_s}-merge-{uuid.uuid4().hex}"
        rel = f"fragments/{new_id}.ndjson"
        tmp_rel = f"tmp/{new_id}.tmp"
        pin_rels = [r["filename"] for r in selected] + [tmp_rel, rel]
        with self._pinned(*pin_rels):
            nrows = 0
            nbytes = 0
            tmp_abs = self.data_dir / tmp_rel
            with open(tmp_abs, "wb") as out:
                for r in selected:
                    with open(self.data_dir / r["filename"], "rb") as f:
                        while True:
                            chunk = f.read(_CHUNK)
                            if not chunk:
                                break
                            nrows += chunk.count(b"\n")
                            nbytes += len(chunk)
                            out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp_abs, self.data_dir / rel)
            self._fsync_dir(self.fragments_dir)

            self._before_merge_commit_hook()  # 测试注入点，默认空操作

            # 3) 单事务提交：仅当选中片段仍全部 active 才替换
            with self._lock:
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    cur = self._db.execute(
                        "UPDATE fragments SET state = 'replaced', replaced_by = ?"
                        f" WHERE state = 'active' AND id IN ({','.join('?' * len(ids))})",
                        (new_id, *ids),
                    )
                    if cur.rowcount != len(ids):
                        self._db.execute("ROLLBACK")
                        raise MergeConflict(
                            "并发合并已替换部分选中片段，本次合并未生效"
                        )
                    created = _utcnow()
                    self._db.execute(
                        "INSERT INTO fragments (id, day, filename, state, rows, bytes, created_at)"
                        " VALUES (?, ?, ?, 'active', ?, ?, ?)",
                        (new_id, day_s, rel, nrows, nbytes, created),
                    )
                    self._db.execute("COMMIT")
                except BaseException:
                    if self._db.in_transaction:
                        self._db.execute("ROLLBACK")
                    raise
                active_count = self._db.execute(
                    "SELECT COUNT(*) FROM fragments WHERE day = ? AND state = 'active'",
                    (day_s,),
                ).fetchone()[0]

        return MergeResult(
            new_fragment=FragmentRecord(new_id, day_s, rel, nrows, nbytes, created),
            replaced_ids=[r["id"] for r in selected],
            active_count=active_count,
        )

    def _before_merge_commit_hook(self) -> None:
        """合并提交前的测试钩子（默认空操作，测试可替换以制造并发交错）。"""

    # ------------------------------------------------------------------
    # 清理：回收废弃文件与已替换的清单行（豁免 pin 住的文件）
    # ------------------------------------------------------------------

    def cleanup(self) -> CleanupResult:
        deleted_files = 0
        deleted_rows = 0
        skipped = 0
        with self._lock:
            referenced = {
                r[0] for r in self._db.execute("SELECT filename FROM fragments")
            }
            replaced = self._db.execute(
                "SELECT id, filename FROM fragments WHERE state = 'replaced'"
            ).fetchall()

            def try_delete(rel: str) -> bool:
                """删除一个废弃文件；返回文件是否已不存在（可继续后续处理）。"""
                nonlocal deleted_files, skipped
                if rel in self._pins:
                    skipped += 1
                    return False
                try:
                    (self.data_dir / rel).unlink()
                    deleted_files += 1
                except FileNotFoundError:
                    pass
                return True

            # tmp 残留：写入未完成的临时文件，从未进入清单
            for p in sorted(self.tmp_dir.iterdir()):
                if p.is_file():
                    try_delete(f"tmp/{p.name}")
            # 孤儿片段：文件已改名但清单未登记（上传/合并提交前崩溃）
            for p in sorted(self.fragments_dir.iterdir()):
                if p.is_file():
                    rel = f"fragments/{p.name}"
                    if rel not in referenced:
                        try_delete(rel)
            # 已被合并替换的片段：删文件后删清单行（崩溃后下次清理可续做）
            for row in replaced:
                if try_delete(row["filename"]):
                    self._db.execute(
                        "DELETE FROM fragments WHERE id = ? AND state = 'replaced'",
                        (row["id"],),
                    )
                    deleted_rows += 1
        return CleanupResult(deleted_files, deleted_rows, skipped)

    def close(self) -> None:
        with self._lock:
            self._db.close()
