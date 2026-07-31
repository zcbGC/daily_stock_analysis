"""决策信号方向 outcome 回填服务（P1-9）。

对 decision_signals 表中的每条信号，取信号日之后 N 个交易日的实际走势，
判断方向命中（hit/miss/neutral）并写入 decision_signal_outcomes 表，
用于统计系统的方向准确率（对应 EDGE_A_SHARE.md 的"衡量 Edge 的指标"）。

数据来源：优先本地 stock_daily 缓存；缓存不足且 fetch=True 时通过
DataFetcherManager 降级链补拉（不写回 DB，避免污染日线缓存）。
"""

import logging
import re
from datetime import date, datetime, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ENGINE_VERSION = "outcome-backfill-v1"
DEFAULT_WINDOWS = [5, 10, 20]

_BULL_ACTIONS = {"buy", "add", "watch", "strong_buy", "hold"}
_BEAR_ACTIONS = {"sell", "reduce", "avoid", "exit"}


def _cn_date(dt: datetime) -> date:
    """把可能为 naive UTC 的时间戳转为中国时区日期。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo("Asia/Shanghai")).date()


def _pure_code(code: str) -> str:
    return re.sub(r"\..*$", "", (code or "")).strip()


def _f(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def expected_direction(action: str) -> Optional[str]:
    """按动作推断期望方向：up=看多 / down=看空 / None=中性（不判方向）。"""
    a = (action or "").strip().lower()
    if a in _BULL_ACTIONS:
        return "up"
    if a in _BEAR_ACTIONS:
        return "down"
    return None


class SignalOutcomeBackfiller:
    """决策信号方向 outcome 回填器。"""

    def __init__(self, db_manager, data_manager=None):
        self._db = db_manager
        self._manager = data_manager

    # ── 数据获取 ──────────────────────────────────────────────

    def _local_bars(self, session, code: str, since: date) -> List[dict]:
        from src.storage import StockDaily
        rows = (
            session.query(StockDaily)
            .filter(StockDaily.code == code, StockDaily.date >= since)
            .order_by(StockDaily.date.asc())
            .all()
        )
        return [
            {"date": r.date, "high": _f(r.high), "low": _f(r.low), "close": _f(r.close)}
            for r in rows
            if r.close is not None
        ]

    def _fetch_bars(self, code: str, since: date) -> Optional[List[dict]]:
        """通过 DataFetcherManager 降级链补拉日线（失败返回 None，不抛异常）。"""
        if self._manager is None:
            return None
        try:
            df, _source = self._manager.get_daily_data(
                code,
                start_date=since.strftime("%Y-%m-%d"),
                end_date=datetime.now().strftime("%Y-%m-%d"),
            )
        except Exception as exc:
            logger.warning("outcome-backfill 补拉日线失败 %s: %s", code, exc)
            return None
        if df is None or df.empty:
            return None
        out = []
        for _, r in df.iterrows():
            d = r.get("date")
            if isinstance(d, str):
                try:
                    d = datetime.strptime(d[:10], "%Y-%m-%d").date()
                except ValueError:
                    continue
            elif not isinstance(d, date):
                continue
            c = _f(r.get("close"))
            if c is None:
                continue
            out.append({"date": d, "high": _f(r.get("high")), "low": _f(r.get("low")), "close": c})
        return sorted(out, key=lambda x: x["date"])

    # ── 窗口计算 ──────────────────────────────────────────────

    def _window_result(self, bars: List[dict], anchor: date, window: int) -> Optional[dict]:
        """以 anchor 当日收盘为基准，计算其后 window 个交易日的窗口结果。

        anchor 当日无日线（非交易日/数据缺失）时，以之后第一个交易日为基准。
        窗口不足返回 None。
        """
        on_anchor = [b for b in bars if b["date"] == anchor]
        if on_anchor:
            start_price = on_anchor[0]["close"]
            future = [b for b in bars if b["date"] > anchor]
        else:
            future = [b for b in bars if b["date"] > anchor]
            if not future:
                return None
            start_price = future[0]["close"]
            future = future[1:]
        if len(future) < window:
            return None
        window_bars = future[:window]
        end_close = window_bars[-1]["close"]
        return {
            "start_price": start_price,
            "end_close": end_close,
            "max_high": max((b["high"] for b in window_bars), default=None),
            "min_low": min((b["low"] for b in window_bars), default=None),
            "stock_return_pct": round((end_close - start_price) / start_price * 100, 2) if start_price else None,
        }

    # ── 主流程 ────────────────────────────────────────────────

    def backfill(self, windows: Optional[List[int]] = None, limit: Optional[int] = None,
                 fetch: bool = True) -> Dict[str, int]:
        from src.storage import DecisionSignalRecord
        windows = windows or DEFAULT_WINDOWS
        stats = {"signals": 0, "evaluated": 0, "insufficient": 0, "upserted": 0}
        with self._db.session_scope() as session:
            q = session.query(DecisionSignalRecord).order_by(DecisionSignalRecord.created_at.asc())
            if limit:
                q = q.limit(limit)
            for sig in q.all():
                stats["signals"] += 1
                anchor = _cn_date(sig.created_at)
                code = _pure_code(sig.stock_code)
                bars = self._local_bars(session, code, anchor)
                if not bars and fetch:
                    fetched = self._fetch_bars(sig.stock_code or code, anchor)
                    if fetched:
                        bars = fetched
                direction = expected_direction(sig.action)
                for w in windows:
                    horizon_key = f"{w}d"
                    res = self._window_result(bars, anchor, w)
                    if res is None:
                        self._upsert(session, sig, horizon_key, anchor, w,
                                     {"eval_status": "insufficient_data",
                                      "unable_reason": "insufficient_future_bars"})
                        stats["insufficient"] += 1
                        continue
                    if direction is None:
                        direction_correct = None
                        outcome = "neutral"
                    else:
                        direction_correct = (
                            res["end_close"] > res["start_price"] if direction == "up"
                            else res["end_close"] < res["start_price"]
                        )
                        outcome = "hit" if direction_correct else "miss"
                    self._upsert(session, sig, horizon_key, anchor, w, {
                        "eval_status": "ok",
                        "outcome": outcome,
                        "direction_expected": direction,
                        "direction_correct": direction_correct,
                        "start_price": res["start_price"],
                        "end_close": res["end_close"],
                        "max_high": res["max_high"],
                        "min_low": res["min_low"],
                        "stock_return_pct": res["stock_return_pct"],
                    })
                    stats["evaluated"] += 1
                    stats["upserted"] += 1
        return stats

    def _upsert(self, session, sig, horizon_key, anchor, window_days, extra: Dict):
        from src.storage import DecisionSignalOutcomeRecord
        existing = (
            session.query(DecisionSignalOutcomeRecord)
            .filter_by(signal_id=sig.id, horizon=horizon_key, engine_version=ENGINE_VERSION)
            .first()
        )
        payload = {
            "signal_id": sig.id,
            "horizon": horizon_key,
            "engine_version": ENGINE_VERSION,
            "anchor_date": anchor,
            "eval_window_days": window_days,
            "action": sig.action,
            "market": sig.market,
            "market_phase": sig.market_phase,
            "source_type": sig.source_type,
            "source_agent": sig.source_agent,
            "plan_quality": sig.plan_quality,
            "holding_state": "unknown",
            **extra,
        }
        if existing:
            for k, v in payload.items():
                setattr(existing, k, v)
        else:
            session.add(DecisionSignalOutcomeRecord(**payload))


def print_outcome_stats(db_manager, windows=None) -> Dict[str, object]:
    """按窗口输出方向命中率统计（只读，供用户判断系统有效性）。"""
    from src.storage import DecisionSignalOutcomeRecord
    windows = windows or DEFAULT_WINDOWS
    result = {"summary": {}, "per_window": {}}
    with db_manager.session_scope() as session:
        total_ok = 0
        total_correct = 0
        for w in windows:
            key = f"{w}d"
            rows = (
                session.query(DecisionSignalOutcomeRecord)
                .filter(DecisionSignalOutcomeRecord.horizon == key,
                        DecisionSignalOutcomeRecord.eval_status == "ok")
                .all()
            )
            judged = [r for r in rows if r.direction_correct is not None]
            if not judged:
                result["per_window"][key] = {"evaluated": 0, "hit_rate": None}
                continue
            correct = sum(1 for r in judged if r.direction_correct)
            result["per_window"][key] = {
                "evaluated": len(judged),
                "hit_rate": round(correct / len(judged) * 100, 1),
            }
            total_ok += len(judged)
            total_correct += correct
        if total_ok:
            result["summary"] = {"evaluated": total_ok,
                                 "hit_rate": round(total_correct / total_ok * 100, 1)}
        else:
            result["summary"] = {"evaluated": 0, "hit_rate": None}
    return result
