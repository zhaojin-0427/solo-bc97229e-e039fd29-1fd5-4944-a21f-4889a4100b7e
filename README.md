# 园区分表电量推算 API

面向园区能源结算场景：采集系统导出的读数在结算分界时刻可能缺测，需要判断
**指定分表在指定起止时刻之间的耗电量能否被现有读数唯一确定**，以决定能否
直接结算、是否需要补抄。

本服务接收 1 个总表与 2~6 个分表的累计电量读数（UTC）及各分表功率上限，
用**线性规划**求该分表在该时段内可行耗电量的**精确最小值与最大值**
（不是线性插值）；读数自相矛盾时明确返回“读数不相容”。

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

## 本地开发与测试

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -q          # 18 个用例：边界求解、区间细分、不相容诊断、入参校验
uvicorn app.main:app --reload       # 本地启动，文档见 http://127.0.0.1:8000/docs
```

## 目录结构

```
app/
  main.py     # FastAPI 路由与文档示例
  models.py   # 请求/响应模型与业务校验
  solver.py   # 线性规划建模、HiGHS 求解、不相容诊断
tests/
  test_api.py # 端到端测试
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```
