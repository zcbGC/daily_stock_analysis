"""盘后综合报告生成服务 — 聚合个股分析 + 大盘复盘 + 持仓快照。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_CN_TZ = timezone(timedelta(hours=8))

EOD_PROMPT = """你是一位 A 股盘后复盘教练。基于今日的数据，生成一份"盘后总结 + 下个开盘日策略"报告。

## 输入数据

### 今日大盘复盘
{market_review}

### 个股分析结果 (JSON)
{stock_results}

### 持仓快照
{portfolio}

## 输出格式

严格输出 JSON，不要额外文字。scenario_a/b/c 必须包含具体价格和量能条件。

{{
  "market_summary": {{
    "one_liner": "一句话总结今日大盘",
    "key_observations": ["观察点1", "观察点2"]
  }},
  "positions": [
    {{
      "code": "股票代码",
      "name": "名称",
      "holding": {{"quantity": 持仓量或0, "avg_cost": 成本或null, "pnl_pct": 浮盈百分比或null}},
      "today": {{"pct_chg": 涨跌幅, "close": 收盘价}},
      "trigger_check": {{
        "stop_loss_triggered": false,
        "stop_loss_price": 止损价或null,
        "distance_to_stop_pct": 距止损百分比或null
      }},
      "strategy": {{
        "scenario_a": {{"condition": "具体条件", "action": "具体操作"}},
        "scenario_b": {{"condition": "具体条件", "action": "具体操作"}},
        "scenario_c": {{"condition": "具体条件", "action": "具体操作"}},
        "hard_stop": 硬止损价格或null
      }}
    }}
  ],
  "risk_summary": {{
    "stop_warnings": ["已触发止损的股票名称"],
    "concentration_alert": "行业集中度警告或null",
    "overall_risk": "整体风险评估"
  }}
}}

## 重要原则
- scenario 中的价格必须来自分析数据中的支撑/压力位，不能编造
- 持仓股策略要更具体（知道成本），非持仓股给出"等待入场条件"
- 如果某只股票分析失败，在 strategy 中标注"分析失败，无法生成策略"
- hard_stop 是最后防线"""


class EODService:
    """盘后综合报告生成 + 持久化。"""

    def __init__(self, db_manager):
        self._db = db_manager

    def generate_summary(
        self,
        stock_results: List[Any],
        market_review: str,
        portfolio: Optional[Dict] = None,
        config: Any = None,
    ) -> Dict[str, Any]:
        """生成盘后综合报告。

        Args:
            stock_results: StockAnalysisPipeline 返回的 AnalysisResult 列表
            market_review: 大盘复盘文字
            portfolio: 持仓快照 (position_id → {symbol, quantity, avg_cost, ...})
            config: DSA 配置对象
        """
        # ① 构建 Prompt
        stocks_json = self._serialize_stock_results(stock_results)
        portfolio_json = json.dumps(portfolio or {}, ensure_ascii=False, indent=2)
        prompt = EOD_PROMPT.format(
            market_review=market_review[:3000],
            stock_results=stocks_json[:8000],
            portfolio=portfolio_json[:2000],
        )

        # ② 调用 LLM
        llm_response = self._call_llm(prompt, config)

        # ③ 解析 + 校验
        report = self._parse_response(llm_response, stock_results)

        # ④ 存储
        self._save(report)

        return report

    def load_latest(self) -> Optional[Dict[str, Any]]:
        row = self._load_latest_row()
        return row.data if row else None

    def load_by_date(self, date: str) -> Optional[Dict[str, Any]]:
        row = self._load_row_by_date(date)
        return row.data if row else None

    # ── internal ────────────────────────────────────────────────

    def _serialize_stock_results(self, results: List[Any]) -> str:
        items = []
        for r in results:
            items.append({
                "code": getattr(r, "code", ""),
                "name": getattr(r, "name", ""),
                "sentiment_score": getattr(r, "sentiment_score", 0),
                "trend_prediction": getattr(r, "trend_prediction", ""),
                "operation_advice": getattr(r, "operation_advice", ""),
                "confidence_level": getattr(r, "confidence_level", ""),
                "analysis_summary": getattr(r, "analysis_summary", "")[:200],
                "dashboard": getattr(r, "dashboard", None),
            })
        return json.dumps(items, ensure_ascii=False, indent=2)

    def _call_llm(self, prompt: str, config: Any = None) -> str:
        """调用 LLM。复用 LiteLLM 通道（LLMToolAdapter.call_text）。"""
        try:
            from src.agent.llm_adapter import LLMToolAdapter
            adapter = LLMToolAdapter(config)
            response = adapter.call_text(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=3000,
            )
            return response.content or ""
        except Exception as e:
            logger.warning("EOD LLM 调用失败: %s", e)
            return "{}"

    def _parse_response(self, raw: str, stock_results: List[Any]) -> Dict[str, Any]:
        """解析 LLM 输出为结构化报告。"""
        # 提取 JSON
        text = raw.strip()
        if "```" in text:
            import re
            m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
            if m:
                text = m.group(1)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning("EOD LLM 返回非 JSON，使用降级格式化输出")
            return self._degraded_report(stock_results)

    def _degraded_report(self, stock_results: List[Any]) -> Dict[str, Any]:
        """LLM 失败时的降级报告 — 纯数据格式化，不含 AI 推理。"""
        positions = []
        for r in (stock_results or []):
            dash = getattr(r, "dashboard", {}) or {}
            battle = dash.get("battle_plan", {}) or {}
            sniper = battle.get("sniper_points", {}) or {}
            positions.append({
                "code": getattr(r, "code", ""),
                "name": getattr(r, "name", ""),
                "holding": {"quantity": None, "avg_cost": None, "pnl_pct": None},
                "today": {"pct_chg": None, "close": None},
                "trigger_check": {
                    "stop_loss_triggered": False,
                    "stop_loss_price": sniper.get("stop_loss"),
                    "distance_to_stop_pct": None,
                },
                "strategy": {
                    "scenario_a": {"condition": "N/A", "action": sniper.get("ideal_buy", "N/A")},
                    "scenario_b": {"condition": "N/A", "action": "持有"},
                    "scenario_c": {"condition": "N/A", "action": sniper.get("stop_loss", "N/A")},
                    "hard_stop": sniper.get("stop_loss"),
                },
            })
        return {
            "market_summary": {"one_liner": "AI 分析不可用，以下为数据摘录", "key_observations": []},
            "positions": positions,
            "risk_summary": {"stop_warnings": [], "concentration_alert": None, "overall_risk": "无法评估"},
            "_degraded": True,
        }

    def _save(self, report: Dict[str, Any]) -> None:
        from src.storage import EODSummary
        today = datetime.now(_CN_TZ).strftime("%Y-%m-%d")
        with self._db.session_scope() as session:
            row = EODSummary(
                date=today,
                next_trading_day=report.get("positions", [{}])[0].get("strategy", {}).get("hard_stop") and today,
                data=report,
                created_at=datetime.now(timezone.utc),
            )
            session.add(row)
        logger.info("EOD 综合报告已保存 date=%s", today)

    def _load_latest_row(self):
        from src.storage import EODSummary
        from sqlalchemy import desc
        with self._db.session_scope() as session:
            return (
                session.query(EODSummary)
                .order_by(desc(EODSummary.created_at))
                .first()
            )

    def _load_row_by_date(self, date: str):
        from src.storage import EODSummary
        with self._db.session_scope() as session:
            return (
                session.query(EODSummary)
                .filter(EODSummary.date == date)
                .first()
            )
