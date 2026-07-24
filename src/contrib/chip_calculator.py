"""
筹码分布本地计算器

基于 go-stock 的换手率衰减 + 高斯核分配算法，Python + numpy 实现。
当东方财富筹码 API (stock_cyq_em) 不可用时，作为本地降级方案。

算法：
1. 遍历每根日K线（从早到晚）
2. 用换手率衰减历史筹码：dist *= (1 - turnover_rate)
3. 以 VWAP 为成本中枢，将当日成交量用高斯核分布在 [low, high] 区间
4. 输出：获利比例、平均成本、90%/70% 筹码集中度
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


def compute_chip_distribution(
    kline_df,
    current_price: float,
    bins: int = 80,
    stock_code: str = "",
) -> Optional[dict]:
    """计算筹码分布。

    Args:
        kline_df: 日K线 DataFrame，需含列: open, high, low, close, volume, amount, turnover_rate
        current_price: 当前价格
        bins: 价格分箱数
        stock_code: 股票代码

    Returns:
        dict with profit_ratio, avg_cost, concentration_90, concentration_70,
        cost_90_low, cost_90_high, cost_70_low, cost_70_high, source
    """
    if kline_df is None or kline_df.empty:
        return None

    df = kline_df.copy()
    df.columns = [str(c).lower() for c in df.columns]

    # 按日期排序
    if "date" in df.columns:
        df = df.sort_values("date").reset_index(drop=True)

    required_days = 20
    if len(df) < required_days:
        return None

    # --- 价格区间和分箱 ---
    price_low = float(df["low"].min())
    price_high = float(df["high"].max())
    if price_high <= price_low:
        price_high = price_low * 1.1
        price_low = price_low * 0.9

    span = price_high - price_low
    bin_width = span / bins
    bin_edges = np.linspace(price_low, price_high, bins + 1)
    dist = np.zeros(bins, dtype=np.float64)

    # 换手率列名适配
    turnover_col = None
    for c in df.columns:
        if "turnover" in c or "换手" in c:
            turnover_col = c
            break
    if turnover_col is None:
        return None

    # 金额列适配
    amount_col = None
    for c in df.columns:
        if "amount" in c or "cje" in c or "成交额" in c:
            amount_col = c
            break
    has_amount = amount_col is not None

    for _, row in df.iterrows():
        lo = float(row["low"])
        hi = float(row["high"])
        vol = float(row["volume"])

        # 解析换手率
        turnover_str = str(row.get(turnover_col, "0"))
        turnover = _parse_turnover(turnover_str)

        # 换手率衰减
        remain = max(0.0, min(1.0 - turnover, 0.98))
        if remain < 1.0:
            dist *= remain

        # 成本中枢：优先 VWAP
        if has_amount and vol > 0:
            amt = float(row[amount_col])
            center = amt / vol if amt > 0 else (hi + lo + float(row["close"])) / 3.0
        else:
            center = (hi + lo + float(row["close"])) / 3.0

        # 高斯核分配
        hi_span = max(hi - lo, 1e-9)
        sigma = max(hi_span * 0.18, hi * 1e-6, 1e-6)

        lo_idx = max(0, int((lo - price_low) / bin_width))
        hi_idx = min(bins - 1, int((hi - price_low) / bin_width))
        if lo_idx > hi_idx:
            lo_idx, hi_idx = hi_idx, lo_idx

        bin_centers = (bin_edges[lo_idx:hi_idx + 1] + bin_edges[lo_idx + 1:hi_idx + 2]) / 2.0
        d = (bin_centers - center) / sigma
        weights = np.exp(-0.5 * d * d)
        w_sum = weights.sum()

        if w_sum > 0 and vol > 0:
            dist[lo_idx:hi_idx + 1] += vol * weights / w_sum

    total_chips = dist.sum()
    if total_chips <= 0:
        return None

    bin_centers_all = (bin_edges[:-1] + bin_edges[1:]) / 2.0

    profit_mask = bin_centers_all < current_price
    profit_ratio = float(dist[profit_mask].sum() / total_chips)
    avg_cost = float(np.average(bin_centers_all, weights=dist))

    cost_90_low, cost_90_high, concentration_90 = _concentration(dist, bin_centers_all, total_chips, 0.90)
    cost_70_low, cost_70_high, concentration_70 = _concentration(dist, bin_centers_all, total_chips, 0.70)

    logger.info(
        f"[ChipCalc] {stock_code}: profit={profit_ratio:.1%}, "
        f"avg={avg_cost:.2f}, conc90={concentration_90:.2%}"
    )

    return {
        "profit_ratio": profit_ratio,
        "avg_cost": avg_cost,
        "cost_90_low": cost_90_low,
        "cost_90_high": cost_90_high,
        "concentration_90": concentration_90,
        "cost_70_low": cost_70_low,
        "cost_70_high": cost_70_high,
        "concentration_70": concentration_70,
        "source": "local_calculated",
    }


def _parse_turnover(value_str: str) -> float:
    value_str = value_str.replace("%", "").strip()
    try:
        v = float(value_str)
        return v / 100.0 if v > 1 else v
    except (ValueError, TypeError):
        return 0.0


def _concentration(dist, centers, total, ratio):
    """滑动窗口计算覆盖 ratio 筹码的最窄价格区间。"""
    target = total * ratio
    best_low, best_high = centers[0], centers[-1]
    best_width = best_high - best_low
    left = 0
    cumsum = 0.0
    for right in range(len(dist)):
        cumsum += dist[right]
        while cumsum - dist[left] >= target and left < right:
            cumsum -= dist[left]
            left += 1
        if cumsum >= target:
            width = centers[right] - centers[left]
            if width < best_width:
                best_width = width
                best_low = centers[left]
                best_high = centers[right]
    concentration = best_width / best_high if best_high > 0 else 1.0
    return float(best_low), float(best_high), float(concentration)
