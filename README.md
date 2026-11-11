# 园区分表电量推算与读数归档 API

面向园区能源结算场景：采集系统导出的读数在结算分界时刻可能缺测，需要判断
**指定分表在指定起止时刻之间的耗电量能否被现有读数唯一确定**，以决定能否
直接结算、是否需要补抄。

本服务接收 1 个总表与 2~6 个分表的累计电量读数（UTC）及各分表功率上限，
用**线性规划**求该分表在该时段内可行耗电量的**精确最小值与最大值**
（不是线性插值）；读数自相矛盾时明确返回“读数不相容”。

同时提供**读数归档 API**：网关频繁上传产生的大量读数小文件，按 UTC 日保存为
独立 NDJSON 片段（本地文件存片段、SQLite 存清单），支持按日下载全部原始行、
合并小片段、片段数查询与废弃文件清理，供下游批量读取原始归档。

## 模型与假设

- 任意时段：总表耗电量 = 全部分表耗电量之和；
- 各分表耗电非负，且不超过 `功率上限 × 时段长度`；
- 不考虑损耗、换表与回零（累计读数单调不减）；
- 每只表均有观测期首、尾读数，中间允许缺测；读数时间统一为 UTC。

**求解方法**：取所有表读数时刻与查询起止时刻的并集切分时间轴，设
`x[j,k]` 为分表 j 在基本区间 k 的耗电量，约束为“每对相邻读数之差 =
区间内耗电之和”“总表 = 分表之和”“`0 ≤ x[j,k] ≤ P_j·Δt_k`”，对查询
窗口内 `x` 之和分别求 min / max（HiGHS 求解器）。

该边界是**精确**的：任何真实功率曲线必满足这些区间约束；反过来任一
LP 可行解都可用不超过功率上限的分段恒定功率物理实现。因此 min/max 就是
全部读数约束下的可行电量边界——线性插值只能给出来历不明的点估计，
无法回答“是否足以确定”。

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

启动后访问交互式 API 文档：**http://localhost:8000/docs**

文档中 `POST /api/v1/infer` 内置三个可直接 *Try it out* 的演示示例：

1. **电量可被唯一确定（无需补抄）**：总表逐小时有读数，01:00~02:00 总表
   耗电 40 kWh，两个分表功率上限各 20 kW → sub_a 该时段被锁定为 20 kWh；
2. **电量只能落在区间内**：同一组读数查询 00:00~01:00 → sub_a 可行范围
   [0, 10] kWh，需结合结算精度判断是否补抄；
3. **读数不相容**：总表 100 kWh 与分表合计 110 kWh 不平衡 → 返回
   `status=incompatible` 及原因诊断。

归档接口（上传、片段数查询、按日下载、合并、清理）同样可在文档中直接操作：
先 `POST /api/v1/archive/fragments` 上传若干片段，再从
`GET /api/v1/archive/fragments` 复制 `fragment_id` 调用
`POST /api/v1/archive/merges` 合并，最后用
`GET /api/v1/archive/days/{day}/download` 下载当日全部原始行。

## API

### `POST /api/v1/infer`

请求体：

```json
{
  "meters": [
    {
      "meter_id": "meter_total",
      "role": "total",
      "readings": [
        {"time": "2026-01-01T00:00:00Z", "value": 1000.0},
        {"time": "2026-01-01T04:00:00Z", "value": 1100.0}
      ]
    },
    {
      "meter_id": "sub_a",
      "role": "sub",
      "max_power_kw": 20.0,
      "readings": [
        {"time": "2026-01-01T00:00:00Z", "value": 500.0},
        {"time": "2026-01-01T04:00:00Z", "value": 560.0}
      ]
    },
    {
      "meter_id": "sub_b",
      "role": "sub",
      "max_power_kw": 20.0,
      "readings": [
        {"time": "2026-01-01T00:00:00Z", "value": 200.0},
        {"time": "2026-01-01T04:00:00Z", "value": 240.0}
      ]
    }
  ],
  "query": {"meter_id": "sub_a", "start": "2026-01-01T01:00:00Z", "end": "2026-01-01T02:00:00Z"}
}
```

- `role`：`total`（总表，恰好 1 个）/ `sub`（分表，2~6 个）；
- `max_power_kw`：仅分表需要，单位 kW；读数 `value` 单位 kWh；
- 时间须带时区（按 UTC 归一化），每只表的首/末读数须对齐观测期首/尾；
- `query` 的起止时刻须落在观测期内且 `start < end`，查询对象须为分表。

响应（可行）：

```json
{
  "status": "ok",
  "meter_id": "sub_a",
  "start": "2026-01-01T01:00:00Z",
  "end": "2026-01-01T02:00:00Z",
  "min_kwh": 20.0,
  "max_kwh": 20.0,
  "is_determined": true,
  "message": "该时段电量可被现有读数唯一确定，可直接结算，无需补抄",
  "diagnostics": []
}
```

响应（读数不相容，HTTP 仍为 200，`status` 区分结果）：

```json
{
  "status": "incompatible",
  "min_kwh": null,
  "max_kwh": null,
  "is_determined": null,
  "message": "读数不相容：……无可行解",
  "diagnostics": ["观测期内总表耗电 100 kWh 与各分表合计 110 kWh 不平衡"]
}
```

入参不合法（分表数量不符、缺观测期首尾读数、查询越界、时间无时区等）
返回 HTTP 422 及具体原因。

## 读数归档 API

网关每次上传的读数行（表 ID、时刻、累计 kWh）须落在**同一 UTC 日**，服务端
将其保存为一个独立 NDJSON 片段；下游按日下载全部原始行（**重复行原样保留**）。
片段文件存本地磁盘，清单（片段 ID、所属日、状态、行数等）存 SQLite。

数据目录由环境变量 `ARCHIVE_DATA_DIR` 指定（默认 `./data`，Docker Compose 中
为挂载命名卷的 `/srv/app/data`）：

```
data/
  manifest.sqlite3   # 片段清单
  fragments/         # 已提交的 NDJSON 片段
  tmp/               # 写入中的临时文件（对读者不可见）
```

### `POST /api/v1/archive/fragments` — 上传

```json
{
  "rows": [
    {"meter_id": "sub_a", "time": "2026-01-01T00:00:00Z", "kwh": 500.0},
    {"meter_id": "sub_a", "time": "2026-01-01T01:00:00Z", "kwh": 510.5}
  ]
}
```

- 所有行的时刻（按 UTC 归一）必须在同一日，否则 422；片段所属日由行时刻推导；
- 返回 `201` 及 `fragment_id` / `day` / `rows` / `bytes`。

### `GET /api/v1/archive/fragments?day=2026-01-01` — 片段数查询

返回当前有效片段数、总行数字节数及片段清单；`day` 缺省时返回所有日的汇总。

### `GET /api/v1/archive/days/{day}/download` — 按日下载

下载**开始时**固定当日的有效片段集合，逐片段拼接输出 NDJSON 原始行
（`Content-Disposition` 附件，`application/x-ndjson`）；下载期间的并发合并
与清理不会打断本次下载。该日无片段时返回 404。

### `POST /api/v1/archive/merges` — 合并

```json
{"day": "2026-01-01", "fragment_ids": ["<fragment_id_1>", "<fragment_id_2>"]}
```

- 把**开始时选定**的片段顺序拼接写成一个新片段，提交只替换选中片段；
- 合并**期间允许上传**，新上传的片段不受影响、予以保留；
- 同组选中片段的并发合并**最多一个生效**：其余返回 `409`（未产生任何清单变更，
  其临时产物由清理回收）。

### `POST /api/v1/archive/cleanup` — 清理废弃文件

删除崩溃残留的临时文件、清单未引用的孤儿文件及已被合并替换的片段文件；
正在进行的上传/合并/下载所涉文件会被跳过（响应中 `skipped_pinned` 计数）。

### 崩溃安全

写文件、切换清单或清理时进程中断，重启后（启动时自动执行一次恢复清理）：

- **每次成功上传的全部行仍各保留一次**：片段先写临时文件、fsync、原子改名，
  再提交 SQLite 清单；清单提交即持久，改名与提交之间崩溃只留孤儿文件；
- **不暴露未写完的文件**：下载与片段查询只走清单，清单引用的文件必然完整；
- **可继续清理废弃文件**：tmp 残留、孤儿文件、已替换片段均可再次清理；
- 合并的清单切换在单个 SQLite 事务中完成（条件更新校验选中片段仍全部有效），
  事务提交前崩溃清单不变，提交后崩溃只遗留待清理的被替换文件。

## 本地开发与测试

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -q          # 42 个用例：推算求解、归档上传/下载/合并/清理、并发与崩溃恢复
uvicorn app.main:app --reload       # 本地启动，文档见 http://127.0.0.1:8000/docs
```

## 目录结构

```
app/
  main.py           # FastAPI 应用与推算路由、文档示例
  models.py         # 推算请求/响应模型与业务校验
  solver.py         # 线性规划建模、HiGHS 求解、不相容诊断
  archive_api.py    # 归档 API 路由（上传/查询/下载/合并/清理）
  archive_models.py # 归档请求/响应模型
  archive_store.py  # 归档存储引擎：NDJSON 片段文件 + SQLite 清单 + 崩溃恢复
tests/
  test_api.py       # 推算接口端到端测试
  test_archive.py   # 归档接口、并发合并与崩溃恢复测试
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```
