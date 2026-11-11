"""电量推算核心求解器。

建模思想（线性规划，求精确可行边界，而非线性插值）：

1. 时间轴：取所有表读数时刻与查询起止时刻的并集，排序去重后得到
   基本区间 k = 0..K-1，区间时长 dt_k（小时）。
2. 决策变量 x[j, k]：分表 j 在基本区间 k 内的耗电量（kWh）。
   总表变量用等式“总表 = 各分表之和”消去。
3. 约束：
   - 每只表的每对相邻读数 (t_a, v_a) -> (t_b, v_b)：
         sum_{k in [t_a, t_b)} x[j, k] = v_b - v_a
   - 总表的每对相邻读数：
         sum_j sum_{k in [t_a, t_b)} x[j, k] = v_b - v_a
   - 0 <= x[j, k] <= P_j * dt_k   （耗电非负且受功率上限约束）
4. 目标：对查询分表 j* 与查询窗口 [q_start, q_end)，分别求
         min / max  sum_{k in [q_start, q_end)} x[j*, k]

精确性说明：任意真实功率曲线必然满足上述区间约束；反之，任一 LP 可行解
都可用分段恒定功率 x[j, k]/dt_k（不超过 P_j）物理实现。因此 LP 给出的
上下界就是全部读数约束下的精确可行边界。查询边界并入时间轴做区间细分后，
目标可精确表达；细分区间上的功率上限约束对任意时段本来就成立，不引入误差。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linprog

MS_PER_HOUR = 3_600_000.0
ROUND_NDIGITS = 6
EPS = 1e-9


@dataclass
class MeterSeries:
    """单只表的累计读数序列（时间已转换为 UTC 毫秒整数，严格递增）。"""

    meter_id: str
    role: str  # "total" | "sub"
    max_power_kw: float | None
    times_ms: list[int]
    values: list[float]  # 与 times_ms 对齐的累计电量读数（kWh）


@dataclass
class SolveResult:
    feasible: bool
    min_kwh: float | None = None
    max_kwh: float | None = None
    diagnostics: list[str] = field(default_factory=list)


def solve_window(
    meters: list[MeterSeries],
    query_meter_id: str,
    q_start_ms: int,
    q_end_ms: int,
) -> SolveResult:
    """求指定分表在 [q_start_ms, q_end_ms) 内耗电量的可行最小/最大值。"""
    subs = [m for m in meters if m.role == "sub"]
    total = next(m for m in meters if m.role == "total")
    j_query = next(i for i, m in enumerate(subs) if m.meter_id == query_meter_id)

    # 1) 时间轴：全部读数时刻 + 查询起止时刻
    cuts = {q_start_ms, q_end_ms}
    for m in meters:
        cuts.update(m.times_ms)
    ts = sorted(cuts)
    pos = {t: i for i, t in enumerate(ts)}
    n_intervals = len(ts) - 1
    dt_h = np.array(
        [(ts[k + 1] - ts[k]) / MS_PER_HOUR for k in range(n_intervals)]
    )

    n_subs = len(subs)
    n_vars = n_subs * n_intervals  # x[j, k] 展平为一维

    # 2) 变量界：0 <= x[j, k] <= P_j * dt_k
    bounds = [
        (0.0, subs[j].max_power_kw * dt_h[k])
        for j in range(n_subs)
        for k in range(n_intervals)
    ]

    # 3) 等式约束：每只表相邻读数之差 = 区间内耗电之和
    a_rows: list[np.ndarray] = []
    b_vals: list[float] = []

    def add_reading_constraints(m: MeterSeries, coef) -> None:
        for k0, k1, delta_v in _reading_deltas(m, pos):
            row = np.zeros(n_vars)
            for j in range(n_subs):
                c = coef(j)
                if c:
                    row[j * n_intervals + k0 : j * n_intervals + k1] = c
            a_rows.append(row)
            b_vals.append(delta_v)

    add_reading_constraints(total, lambda j: 1.0)  # 总表 = 各分表之和
    for j0, m in enumerate(subs):  # 各分表自身读数
        add_reading_constraints(m, lambda j, j0=j0: 1.0 if j == j0 else 0.0)

    a_eq = np.vstack(a_rows)
    b_eq = np.asarray(b_vals)

    # 4) 目标：查询分表在查询窗口内的耗电量
    c = np.zeros(n_vars)
    c[
        j_query * n_intervals + pos[q_start_ms] : j_query * n_intervals
        + pos[q_end_ms]
    ] = 1.0

    res_min = linprog(c, A_eq=a_eq, b_eq=b_eq, bounds=bounds, method="highs")
    if res_min.status == 2:
        # 不可行：读数不相容
        return SolveResult(feasible=False, diagnostics=diagnose(meters))
    if res_min.status != 0:
        raise RuntimeError(f"线性规划求解失败（求最小值）：{res_min.message}")

    res_max = linprog(-c, A_eq=a_eq, b_eq=b_eq, bounds=bounds, method="highs")
    if res_max.status != 0:
        raise RuntimeError(f"线性规划求解失败（求最大值）：{res_max.message}")

    min_v = _clean(res_min.fun)
    max_v = _clean(-res_max.fun)
    if max_v < min_v:  # 数值抖动保护
        max_v = min_v
    return SolveResult(feasible=True, min_kwh=min_v, max_kwh=max_v)


def _reading_deltas(m: MeterSeries, pos: dict[int, int]):
    """生成该表每对相邻读数对应的基本区间范围 [k0, k1) 与读数差。"""
    for i in range(len(m.times_ms) - 1):
        t0, t1 = m.times_ms[i], m.times_ms[i + 1]
        yield pos[t0], pos[t1], m.values[i + 1] - m.values[i]


def _clean(v: float) -> float:
    if abs(v) < EPS:
        v = 0.0
    return round(float(v), ROUND_NDIGITS)


def diagnose(meters: list[MeterSeries]) -> list[str]:
    """LP 不可行时给出人类可读的不相容原因（尽力而为的必要条件检查）。"""
    issues: list[str] = []
    subs = [m for m in meters if m.role == "sub"]
    total = next(m for m in meters if m.role == "total")

    def fmt_t(ms: int) -> str:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    # a) 累计读数必须单调不减（不考虑换表与回零）
    for m in meters:
        for i in range(len(m.values) - 1):
            if m.values[i + 1] < m.values[i] - EPS:
                issues.append(
                    f"表 {m.meter_id} 读数由 {m.values[i]} kWh 降至 "
                    f"{m.values[i + 1]} kWh（{fmt_t(m.times_ms[i])} -> "
                    f"{fmt_t(m.times_ms[i + 1])}），与“不换表、不回零”矛盾"
                )

    # b) 分表读数增量不得超过功率上限
    for m in subs:
        for i in range(len(m.values) - 1):
            hours = (m.times_ms[i + 1] - m.times_ms[i]) / MS_PER_HOUR
            cap = m.max_power_kw * hours
            delta = m.values[i + 1] - m.values[i]
            if delta > cap + EPS:
                issues.append(
                    f"分表 {m.meter_id} 在 {fmt_t(m.times_ms[i])} -> "
                    f"{fmt_t(m.times_ms[i + 1])} 耗电 {round(delta, 6)} kWh，"
                    f"超出功率上限 {m.max_power_kw} kW 在该时段的最大可能值 "
                    f"{round(cap, 6)} kWh"
                )

    # c) 整个观测期：总表增量必须等于各分表增量之和
    d_total = total.values[-1] - total.values[0]
    d_subs = sum(m.values[-1] - m.values[0] for m in subs)
    if abs(d_total - d_subs) > 1e-6:
        issues.append(
            f"观测期内总表耗电 {round(d_total, 6)} kWh 与 "
            f"各分表合计 {round(d_subs, 6)} kWh 不平衡"
        )

    # d) 总表每个读数区间的耗电不得超过各分表功率上限之和
    cap_total = sum(m.max_power_kw for m in subs)
    for i in range(len(total.values) - 1):
        hours = (total.times_ms[i + 1] - total.times_ms[i]) / MS_PER_HOUR
        delta = total.values[i + 1] - total.values[i]
        if delta > cap_total * hours + EPS:
            issues.append(
                f"总表在 {fmt_t(total.times_ms[i])} -> "
                f"{fmt_t(total.times_ms[i + 1])} 耗电 {round(delta, 6)} kWh，"
                f"超出全部分表功率上限之和 {cap_total} kW 在该时段的最大可能值 "
                f"{round(cap_total * hours, 6)} kWh"
            )

    return issues
