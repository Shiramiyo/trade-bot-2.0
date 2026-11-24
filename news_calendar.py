"""Economic news calendar and Monday / high-impact news session filtering."""

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo
import pandas as pd

from config import (
    CURRENCY_TO_SESSION,
    CURRENCY_TO_SYMBOLS,
    SESSIONS,
    TIMEZONE,
    StrategyConfig,
)
from models import NewsEvent

NY_TZ = ZoneInfo(TIMEZONE)


class NewsCalendar:
    """Loads high-impact news events and evaluates session/symbol eligibility."""

    def __init__(self, events: Optional[List[NewsEvent]] = None) -> None:
        self.events: List[NewsEvent] = events or []
        self._by_date: Dict[date, List[NewsEvent]] = {}
        for ev in self.events:
            self._by_date.setdefault(ev.timestamp.date(), []).append(ev)

    @classmethod
    def from_csv(cls, csv_path: str | Path) -> "NewsCalendar":
        """
        Load high-impact news from CSV with columns: timestamp, currency, title, impact.
        Timestamps without timezone are assumed to be America/New_York.
        """
        df = pd.read_csv(csv_path)
        events: List[NewsEvent] = []
        for _, row in df.iterrows():
            impact = str(row.get("impact", "HIGH")).upper()
            if impact not in ("HIGH", "RED"):
                continue
            ts = pd.Timestamp(row["timestamp"])
            if ts.tzinfo is None:
                ts = ts.tz_localize(NY_TZ)
            else:
                ts = ts.tz_convert(NY_TZ)
            events.append(
                NewsEvent(
                    timestamp=ts.to_pydatetime(),
                    currency=str(row["currency"]).upper().strip(),
                    title=str(row.get("title", "High Impact News")),
                    impact=impact,
                )
            )
        return cls(events)

    def events_on_date(self, d: date) -> List[NewsEvent]:
        return self._by_date.get(d, [])

    def check_eligibility(
        self,
        ts: datetime,
        symbol: str,
        cfg: StrategyConfig,
    ) -> Tuple[bool, str, float, bool, Optional[datetime]]:
        """
        Evaluate whether `symbol` can be traded at `ts` (America/New_York).
        Returns: (allowed, session_name, risk_pct_override_or_0, is_news_trade, news_close_at)
        - risk_pct_override_or_0: >0 if Monday no-news forces 0.5%, else 0.0 (use normal/ranging).
        """
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=NY_TZ)
        else:
            ts = ts.astimezone(NY_TZ)

        # Monday (0) to Friday (4) only; optionally skip Monday completely
        if ts.weekday() >= 5 or (cfg.skip_monday and ts.weekday() == 0):
            return False, "", 0.0, False, None

        # Hard rule: no trading in the last N days of the year (holidays, Dec 27-31)
        if (
            cfg.year_end_blackout_days > 0
            and (date(ts.year, 12, 31) - ts.date()).days < cfg.year_end_blackout_days
        ):
            return False, "", 0.0, False, None

        day_events = self.events_on_date(ts.date())
        if cfg.news_only and not day_events:
            return False, "", 0.0, False, None
        usd_events = [e for e in day_events if e.currency == "USD"]
        non_usd_events = [e for e in day_events if e.currency != "USD"]

        # 1. USD high-impact news day:
        # Only trade USD-related pairs within [news - 1h, news + 1h] (or NY if ny_only).
        if usd_events:
            for ev in usd_events:
                win_start = ev.timestamp - timedelta(minutes=cfg.news_window_minutes)
                win_end = ev.timestamp + timedelta(minutes=cfg.news_window_minutes)
                if win_start <= ts <= win_end:
                    if symbol in CURRENCY_TO_SYMBOLS.get("USD", []):
                        close_at = win_end if cfg.force_close_news_1h else None
                        return True, "USD_NEWS", 0.0, True, close_at
            return False, "", 0.0, False, None

        # Determine active standard session ([open - 1h, open + 1h])
        active_session = self._get_active_session(ts)
        if active_session is None:
            return False, "", 0.0, False, None

        if cfg.ny_only and active_session != "NEW_YORK":
            return False, "", 0.0, False, None

        # 2. Non-USD high-impact news day (e.g., AUD, JPY, NZD, GBP, EUR):
        # Focus on the session with that news AND New York session; skip other sessions.
        if non_usd_events:
            news_sessions = {
                CURRENCY_TO_SESSION[e.currency]
                for e in non_usd_events
                if e.currency in CURRENCY_TO_SESSION
            }
            allowed_sessions = news_sessions | {"NEW_YORK"}
            if active_session not in allowed_sessions:
                return False, "", 0.0, False, None

            if active_session in news_sessions:
                affected_symbols = set()
                for e in non_usd_events:
                    if CURRENCY_TO_SESSION.get(e.currency) == active_session:
                        affected_symbols.update(CURRENCY_TO_SYMBOLS.get(e.currency, []))
                if symbol not in affected_symbols:
                    return False, "", 0.0, False, None
                return True, active_session, 0.0, True, None

            # New York session on non-USD news day
            if symbol in SESSIONS["NEW_YORK"].symbols:
                return True, "NEW_YORK", 0.0, False, None
            return False, "", 0.0, False, None

        # 3. Monday with NO high-impact news:
        # New York session only, half risk (0.5%), max 2 trades total.
        if ts.weekday() == 0:
            if active_session != "NEW_YORK":
                return False, "", 0.0, False, None
            if symbol in SESSIONS["NEW_YORK"].symbols:
                return True, "NEW_YORK", cfg.monday_no_news_risk_pct, False, None
            return False, "", 0.0, False, None

        # 4. Normal Tue-Fri without high-impact news:
        if symbol in SESSIONS[active_session].symbols:
            return True, active_session, 0.0, False, None

        return False, "", 0.0, False, None

    @staticmethod
    def _get_active_session(ts: datetime) -> Optional[str]:
        """Return session name if `ts` is within [open - 1h, open + 1h] in NY time."""
        for name, sess in SESSIONS.items():
            # Anchor open_time to ts's date, also check previous/next day for Asia 19:00 wrap
            for day_offset in (0, -1, 1):
                anchor_date = ts.date() + timedelta(days=day_offset)
                open_dt = datetime.combine(anchor_date, sess.open_time, tzinfo=NY_TZ)
                start_dt = open_dt - timedelta(minutes=sess.pre_open_minutes)
                end_dt = open_dt + timedelta(minutes=sess.post_open_minutes)
                if start_dt <= ts <= end_dt:
                    return name
        return None
