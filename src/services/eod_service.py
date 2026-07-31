"""盘后综合报告生成 — 优先 LLM，失败则用结构化数据格式化高质量报告。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)
_CN_TZ = timezone(timedelta(hours=8))


class EODService:
    """盘后综合报告生成 + 持久化。"""

    def __init__(self, db_manager):
        self._db = db_manager

    def generate_summary(
        self, stock_results, market_review: str,
        portfolio: Optional[Dict] = None, config: Any = None,
    ) -> Dict[str, Any]:
        # 始终以结构化数据为基础（分数/价格/均线等事实数据）
        report = self._build_structured_report(stock_results, market_review, portfolio)
        # LLM 增强策略和市场总结（可选，失败不影响基础数据）
        llm = self._try_llm(stock_results, market_review, portfolio, config)
        if llm:
            report["market_summary"] = llm.get("market_summary", report["market_summary"])
            report["risk_summary"] = llm.get("risk_summary", report["risk_summary"])
            # 用 LLM 的 strategy 覆盖结构化版本
            llm_positions = {p["code"]: p for p in llm.get("positions", []) or []}
            for p in report["positions"]:
                lp = llm_positions.get(p["code"])
                if lp and lp.get("strategy"):
                    p["strategy"] = lp["strategy"]
            report["_llm_enhanced"] = True
        self._save(report)
        return report

    # ── LLM 路径 ────────────────────────────────────────────────

    def _try_llm(self, stock_results, market_review, portfolio, config) -> Optional[Dict]:
        try:
            prompt = self._build_llm_prompt(stock_results, market_review, portfolio)
            raw = self._call_llm(prompt, config)
            return self._parse_response(raw, stock_results)
        except Exception as e:
            logger.warning("EOD LLM 失败: %s", e)
            return None

    def _build_llm_prompt(self, stock_results, market_review, portfolio) -> str:
        stocks_json = json.dumps([self._extract_stock_brief(r) for r in (stock_results or [])], ensure_ascii=False, indent=2)
        return f"""你是A股盘后复盘教练。**仅输出JSON，不要文字说明。**

## 大盘
{market_review[:2000]}

## 个股
{stocks_json[:6000]}

## 持仓
{json.dumps(portfolio or {}, ensure_ascii=False)[:1000]}

输出格式: {{"market_summary":{{"one_liner":"...","key_observations":[...]}},"positions":[{{"code":"...","strategy":{{"scenario_a":{{"condition":"...","action":"..."}},"scenario_b":{{...}},"scenario_c":{{...}},"hard_stop":...}}}}],"risk_summary":{{"overall_risk":"..."}}}}"""

    def _call_llm(self, prompt: str, config: Any = None) -> str:
        from src.agent.llm_adapter import LLMToolAdapter
        adapter = LLMToolAdapter(config)
        response = adapter.call_text(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3, max_tokens=2500,
        )
        return response.content or ""

    def _parse_response(self, raw: str, stock_results) -> Optional[Dict]:
        text = (raw or "").strip()
        if not text: return None
        import re
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
        if m: text = m.group(1)
        else:
            s = text.find("{"); e = text.rfind("}")
            if s >= 0 and e > s: text = text[s:e+1]
        try: return json.loads(text)
        except json.JSONDecodeError: return None

    # ── 结构化报告路径（LLM 失败时用） ──────────────────────────

    def _build_structured_report(self, stock_results, market_review, portfolio) -> Dict:
        """用已有分析数据构建高质量报告。不需要 AI。"""
        positions = []
        for r in (stock_results or []):
            positions.append(self._extract_stock_detail(r, portfolio))
        return {
            "market_summary": self._extract_market_brief(market_review),
            "positions": positions,
            "risk_summary": self._extract_risk(positions),
            "_structured": True,
        }

    def _extract_stock_brief(self, r) -> Dict:
        """从 AnalysisResult 提取简要数据（给 LLM Prompt 用）。"""
        dash = getattr(r, "dashboard", {}) or {}
        pp = (dash.get("data_perspective", {}) or {}).get("price_position", {}) or {}
        battle = (dash.get("battle_plan", {}) or {}).get("sniper_points", {}) or {}
        return {
            "code": getattr(r, "code", ""), "name": getattr(r, "name", ""),
            "score": getattr(r, "sentiment_score", 0), "trend": getattr(r, "trend_prediction", ""),
            "advice": getattr(r, "operation_advice", ""),
            "close": getattr(r, "current_price", None),
            "pct_chg": getattr(r, "change_pct", None),
            "support": pp.get("support_level"), "resistance": pp.get("resistance_level"),
            "stop_loss": self._clean(battle.get("stop_loss")),
            "ideal_buy": self._clean(battle.get("ideal_buy")),
            "summary": (getattr(r, "analysis_summary", "") or "")[:150],
        }

    def _extract_stock_detail(self, r, portfolio: Dict) -> Dict:
        """从 AnalysisResult 提取详细数据（给格式化报告用）。"""
        code = getattr(r, "code", "")
        name = getattr(r, "name", "")
        dash = getattr(r, "dashboard", {}) or {}
        dp = dash.get("data_perspective", {}) or {}
        tr = dp.get("trend_status", {}) or {}
        pp = dp.get("price_position", {}) or {}
        vol = dp.get("volume_analysis", {}) or {}
        chip = dp.get("chip_structure", {}) or {}
        cf = dp.get("capital_flow", {}) or {}
        intel = dash.get("intelligence", {}) or {}
        battle = dash.get("battle_plan", {}) or {}
        sniper = battle.get("sniper_points", {}) or {}
        # 价格数据
        close = self._n(getattr(r, "current_price", None))
        pct_chg = self._n(getattr(r, "change_pct", None))
        if close is None:
            ms = getattr(r, "market_snapshot", {}) or {}
            close = ms.get("price")
            pct_chg_val = ms.get("change_pct")
            if pct_chg_val is not None:
                try: pct_chg = float(pct_chg_val)
                except: pass
        # 持仓数据
        holding = {}
        p_code = code.replace(".SH","").replace(".SZ","").replace(".BJ","")
        if portfolio and p_code in portfolio:
            h = portfolio[p_code]
            quantity = h.get("quantity", 0)
            avg_cost = h.get("avg_cost")
            pnl = None
            if avg_cost and close and quantity:
                pnl = round((float(close) - float(avg_cost)) / float(avg_cost) * 100, 1)
            holding = {"quantity": quantity, "avg_cost": avg_cost, "pnl_pct": pnl}
        # 止损距
        stop = self._clean(sniper.get("stop_loss"))
        sl_pct = None
        if stop and close:
            try: sl_pct = round(abs(float(close) - float(stop)) / float(close) * 100, 1)
            except: pass
        return {
            "code": code, "name": name,
            "score": getattr(r, "sentiment_score", 0),
            "trend": getattr(r, "trend_prediction", ""),
            "advice": getattr(r, "operation_advice", ""),
            "confidence": getattr(r, "confidence_level", ""),
            "close": close, "pct_chg": pct_chg,
            "ma": {"alignment": tr.get("ma_alignment", ""), "bullish": tr.get("is_bullish", False), "ma5": pp.get("ma5"), "ma10": pp.get("ma10"), "ma20": pp.get("ma20")},
            "support": pp.get("support_level"), "resistance": pp.get("resistance_level"),
            "volume": {"ratio": vol.get("volume_ratio"), "status": vol.get("volume_status", ""), "meaning": vol.get("volume_meaning", "")},
            "chip": {"profit_ratio": chip.get("profit_ratio"), "avg_cost": chip.get("avg_cost"), "concentration": chip.get("concentration")},
            "capital_verdict": cf.get("capital_verdict", "数据缺失"),
            "stop_loss": stop, "stop_distance_pct": sl_pct,
            "ideal_buy": self._clean(sniper.get("ideal_buy")),
            "risk_alerts": intel.get("risk_alerts", [])[:3],
            "sentiment": intel.get("sentiment_summary", ""),
            "summary": (getattr(r, "analysis_summary", "") or "")[:200],
            "holding": holding,
            "strategy": {
                "scenario_a": {"condition": f"突破压力位 {pp.get('resistance_level') or '?'}", "action": f"入场参考: {self._clean(sniper.get('ideal_buy')) or '等待回踩'}"},
                "scenario_b": {"condition": f"在 {pp.get('support_level') or '?'} ~ {pp.get('resistance_level') or '?'} 区间", "action": "持有不动"},
                "scenario_c": {"condition": f"跌破支撑 {pp.get('support_level') or '?'}", "action": f"止损: {stop or '未设置'}"},
                "hard_stop": stop or "未设置",
            },
        }

    def _extract_market_brief(self, text: str) -> Dict:
        """从大盘复盘文本提取简要信息。"""
        lines = (text or "").split("\n")
        return {"one_liner": next((l.strip("- ").strip() for l in lines if l.strip().startswith("- ")), "无"), "key_observations": [], "_raw": text[:500]}

    def _extract_risk(self, positions) -> Dict:
        triggered = [p["name"] for p in positions if p.get("stop_distance_pct") is not None and p["stop_distance_pct"] <= 0]
        close = [p["name"] for p in positions if p.get("stop_distance_pct") is not None and 0 < p["stop_distance_pct"] <= 10]
        return {"stop_warnings": close, "stop_triggered": triggered, "concentration_alert": None, "overall_risk": "正常" if not triggered else f"⚠ {len(triggered)}只触发止损"}

    # ── 工具方法 ────────────────────────────────────────────────

    @staticmethod
    def _n(v): return v

    @staticmethod
    def _clean(v):
        if v is None: return None
        if isinstance(v, (int, float)): return str(v)
        v = str(v)
        for p in ['理想买入点：','次优买入点：','止损位：','目标位：','理想入场位：','理想买入点：','止损位：',
                   'Ideal Entry:','Stop Loss:','Target:','理想买入点:', '次优买入点:', '止损位:', '目标位:']:
            if v.startswith(p): return v[len(p):]
        return v

    def _save(self, report: Dict) -> None:
        from src.storage import EODSummary
        today = datetime.now(_CN_TZ).strftime("%Y-%m-%d")
        with self._db.session_scope() as session:
            session.add(EODSummary(date=today, next_trading_day=today, data=report, created_at=datetime.now(timezone.utc)))
        logger.info("EOD 综合报告已保存 date=%s", today)

    def load_latest(self) -> Optional[Dict]:
        from src.storage import EODSummary
        from sqlalchemy import desc
        with self._db.session_scope() as s:
            row = s.query(EODSummary).order_by(desc(EODSummary.created_at)).first()
            return row.data if row else None

    def load_by_date(self, date: str) -> Optional[Dict]:
        from src.storage import EODSummary
        with self._db.session_scope() as s:
            row = s.query(EODSummary).filter(EODSummary.date == date).first()
            return row.data if row else None
