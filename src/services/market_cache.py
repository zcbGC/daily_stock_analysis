"""大盘数据缓存层 — 管理 market_archive 表的读写和 TTL 判断。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

PHASE_INTRADAY = "intraday"
PHASE_LUNCH_BREAK = "lunch_break"
PHASE_POSTMARKET = "postmarket"
PHASE_NON_TRADING = "non_trading"

_CN_TZ = timezone(timedelta(hours=8))


def _model():
    """延迟导入，避免循环依赖。"""
    from src.storage import MarketArchive
    return MarketArchive


class MarketCacheService:
    """大盘数据缓存层。

    盘中 (intraday): TTL 10 分钟
    午休 (lunch_break): TTL 到下午 13:00
    收盘后 (postmarket): 当日 archived=true → 永远 fresh
    非交易日 (non_trading): 最近交易日 archived=true → 永远 fresh
    """

    CACHE_TTL_INTRADAY = 600
    CACHE_TTL_LUNCH = 5400

    def __init__(self, db_manager):
        self._db = db_manager

    def is_fresh(self, phase: str, region: str = "cn") -> bool:
        row = self._load_latest_row(region)
        if row is None:
            return False
        archived, created_at, row_date, _ = row
        # 非交易日: 最近交易日的归档快照即为可用（本日无新数据可拉）。
        if phase == PHASE_NON_TRADING:
            return archived
        # 归档快照只有在“属于今天”时才被视为永远新鲜，避免昨日/历史
        # 归档行阻塞新交易日的大盘数据拉取。
        if archived and row_date == self._today_str():
            return True
        if phase == PHASE_LUNCH_BREAK:
            ttl = self.CACHE_TTL_LUNCH
        else:
            ttl = self.CACHE_TTL_INTRADAY
        age = (datetime.now(timezone.utc) - created_at.replace(tzinfo=timezone.utc)).total_seconds()
        return age < ttl

    def save_snapshot(self, data: dict, review_text: str = "", phase: str = PHASE_INTRADAY, region: str = "cn") -> None:
        archived = phase in (PHASE_POSTMARKET, PHASE_NON_TRADING)
        M = _model()
        row = M(
            date=self._today_str(), region=region, phase=phase,
            archived=archived, data=data, review_text=review_text,
            created_at=datetime.now(timezone.utc),
        )
        with self._db.session_scope() as session:
            session.add(row)
        logger.info("大盘快照已保存 date=%s phase=%s archived=%s", self._today_str(), phase, archived)

    def load_latest(self, region: str = "cn") -> Optional[dict]:
        row = self._load_latest_row(region)
        return row[3] if row else None  # (archived, created_at, date, data)

    def load_by_date(self, date: str, region: str = "cn") -> Optional[dict]:
        row = self._load_row_by_date(date, region)
        return row[3] if row else None

    def archive_today(self, region: str = "cn") -> None:
        today = self._today_str()
        M = _model()
        with self._db.session_scope() as session:
            row = (
                session.query(M)
                .filter(M.date == today, M.region == region)
                .order_by(M.created_at.desc())
                .first()
            )
            if row:
                row.archived = True
                logger.info("大盘快照已归档 date=%s", today)

    @staticmethod
    def _today_str() -> str:
        return datetime.now(_CN_TZ).strftime("%Y-%m-%d")

    def _load_latest_row(self, region: str = "cn"):
        from sqlalchemy import desc
        M = _model()
        with self._db.session_scope() as session:
            row = (
                session.query(M.archived, M.created_at, M.date, M.data)
                .filter(M.region == region)
                .order_by(desc(M.archived), desc(M.created_at))
                .first()
            )
            return tuple(row) if row else None

    def _load_row_by_date(self, date: str, region: str = "cn"):
        M = _model()
        with self._db.session_scope() as session:
            row = (
                session.query(M.archived, M.created_at, M.date, M.data)
                .filter(M.date == date, M.region == region)
                .order_by(M.created_at.desc())
                .first()
            )
            return tuple(row) if row else None
