"""Event-driven 5-minute multi-symbol backtesting engine."""

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import pandas as pd

from config import SESSIONS, SYMBOL_COSTS, StrategyConfig
from indicators.aoi import detect_liquidity_levels, get_active_1h_aois
from indicators.bias import determine_daily_bias, is_ranging_day
from indicators.cisd import detect_5m_cisd, detect_5m_momentum_aoi
from models import Bias, CISDSignal, Trade, TradeStatus
from news_calendar import NewsCalendar


class Backtester:
    """Simulates the ICT/SMC strategy bar-by-bar across all configured symbols."""

    def __init__(
        self,
        data: Dict[str, Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]],
        cfg: Optional[StrategyConfig] = None,
        news: Optional[NewsCalendar] = None,
    ) -> None:
        self.data = data
        self.cfg = cfg or StrategyConfig()
        self.news = news or NewsCalendar()

        self.equity = self.cfg.initial_capital
        self.equity_curve: List[Tuple[datetime, float]] = []
        self.trades: List[Trade] = []
        self._next_trade_id = 1

        # Track trades per (date, session), per Monday, and whether 1H AOI was tapped today
        self._session_counts: Dict[Tuple, int] = defaultdict(int)
        self._day_counts: Dict = defaultdict(int)
        self._daily_1h_aoi_tapped: Dict[Tuple, bool] = defaultdict(bool)

    def run(self) -> List[Trade]:
        """Run the backtest across all symbols in chronological 5m order."""
        # Build unified sorted timeline of 5m timestamps
        all_times = sorted(
            {ts for df_5m, _, _ in self.data.values() for ts in df_5m.index}
        )
        if not all_times:
            return []

        # Pre-index positions per symbol for fast slicing
        pos_maps = {
            sym: {ts: idx for idx, ts in enumerate(df_5m.index)}
            for sym, (df_5m, _, _) in self.data.items()
        }

        for ts in all_times:
            ts_dt = ts.to_pydatetime()
            for sym, (df_5m, df_1h, df_1d) in self.data.items():
                idx = pos_maps[sym].get(ts)
                if idx is None or idx < 20:
                    continue

                bar = df_5m.iloc[idx]
                window_5m = df_5m.iloc[max(0, idx - 60) : idx + 1]

                # 1. Manage existing pending / open trades for this symbol
                self._manage_symbol_trades(sym, ts_dt, bar, window_5m)

                # 2. Check if we already have an active pending/open trade on this symbol
                if any(
                    t.symbol == sym and t.status in (TradeStatus.PENDING, TradeStatus.OPEN)
                    for t in self.trades
                ):
                    continue

                # 3. Check news & session eligibility
                allowed, session_name, risk_override, is_news, news_close_at = (
                    self.news.check_eligibility(ts_dt, sym, self.cfg)
                )
                if not allowed:
                    continue

                # Enforce per-session & Monday trade caps
                if not self._check_trade_caps(ts_dt, session_name, risk_override > 0):
                    continue

                # 4. Daily bias & ranging day check
                bias = determine_daily_bias(df_1d, df_1h, ts, self.cfg.doji_body_ratio)
                if bias == Bias.NONE:
                    continue

                ranging = is_ranging_day(df_1h, ts, self.cfg.ranging_lookback_days)
                if risk_override > 0:
                    risk_pct = risk_override
                elif ranging:
                    risk_pct = self.cfg.ranging_risk_pct
                else:
                    risk_pct = self.cfg.base_risk_pct

                # 5. Find 1H AOI (FVG or OB) and check if price tapped it, or fallback to 5m momentum
                aois_1h = get_active_1h_aois(df_1h, ts, bias)
                tapped_aoi = None
                for aoi in reversed(aois_1h):
                    if aoi.contains_price(float(bar["low"]), float(bar["high"])):
                        tapped_aoi = aoi
                        self._daily_1h_aoi_tapped[(ts_dt.date(), sym)] = True
                        break

                signal: Optional[CISDSignal] = None
                if tapped_aoi is not None:
                    signal = detect_5m_cisd(
                        window_5m,
                        bias,
                        aoi_source=tapped_aoi,
                        swing_lookback=self.cfg.swing_lookback,
                        is_momentum=False,
                        min_body_ratio=self.cfg.cisd_min_body_ratio,
                        require_sweep=self.cfg.require_5m_sweep_for_cisd,
                    )
                elif self._daily_1h_aoi_tapped[(ts_dt.date(), sym)]:
                    # Momentum fallback ONLY if a 1H AOI was already tapped earlier today
                    mom_aoi = detect_5m_momentum_aoi(window_5m, bias)
                    if mom_aoi is not None:
                        signal = detect_5m_cisd(
                            window_5m,
                            bias,
                            aoi_source=mom_aoi,
                            swing_lookback=self.cfg.swing_lookback,
                            is_momentum=True,
                            min_body_ratio=self.cfg.cisd_min_body_ratio,
                            require_sweep=self.cfg.require_5m_sweep_for_cisd,
                        )

                if signal is None:
                    continue

                # 6. Compute TP (2 RR minimum, or clear liquidity level if > 2 RR)
                tp_price = self._select_tp(signal, df_1h, ts)
                self._place_limit_order(
                    sym=sym,
                    session_name=session_name,
                    signal=signal,
                    tp_price=tp_price,
                    risk_pct=risk_pct,
                    placed_at=ts_dt,
                    is_news=is_news,
                    news_close_at=news_close_at,
                )

            self.equity_curve.append((ts_dt, self.equity))

        return self.trades

    def _check_trade_caps(self, ts: datetime, session_name: str, is_monday_no_news: bool) -> bool:
        day_key = ts.date()
        if is_monday_no_news and self._day_counts[day_key] >= 2:
            return False
        sess_cfg = SESSIONS.get(session_name)
        max_trades = sess_cfg.max_trades if sess_cfg else 2
        return self._session_counts[(day_key, session_name)] < max_trades

    def _select_tp(self, signal: CISDSignal, df_1h: pd.DataFrame, ts: pd.Timestamp) -> float:
        risk_dist = abs(signal.ob_body_level - signal.sl_price)
        default_tp = (
            signal.ob_body_level + risk_dist * self.cfg.default_rr
            if signal.direction == Bias.BULLISH
            else signal.ob_body_level - risk_dist * self.cfg.default_rr
        )

        levels = detect_liquidity_levels(df_1h, ts, self.cfg.eq_tolerance_atr)
        target_side = "HIGH" if signal.direction == Bias.BULLISH else "LOW"
        candidates = []

        for lvl in levels:
            if lvl.side != target_side:
                continue
            if signal.direction == Bias.BULLISH and lvl.price > default_tp:
                candidates.append(lvl.price)
            elif signal.direction == Bias.BEARISH and lvl.price < default_tp:
                candidates.append(lvl.price)

        if not candidates:
            return default_tp

        # Pick nearest clear liquidity level beyond 2R
        return min(candidates) if signal.direction == Bias.BULLISH else max(candidates)

    def _place_limit_order(
        self,
        sym: str,
        session_name: str,
        signal: CISDSignal,
        tp_price: float,
        risk_pct: float,
        placed_at: datetime,
        is_news: bool,
        news_close_at: Optional[datetime],
    ) -> None:
        risk_dist = abs(signal.ob_body_level - signal.sl_price)
        if risk_dist <= 0:
            return

        risk_amount = self.equity * risk_pct
        size = risk_amount / risk_dist

        # Cost / micro-SL guard: skip trade if spread+slippage+commission > max_cost_to_risk_ratio
        if self.cfg.apply_costs:
            sc = SYMBOL_COSTS.get(sym)
            if sc is not None and self.cfg.max_cost_to_risk_ratio > 0:
                est_costs = (sc.spread + 2.0 * sc.slippage_per_side) * size + (
                    2.0 * sc.commission_rate * signal.ob_body_level * size
                )
                if est_costs > risk_amount * self.cfg.max_cost_to_risk_ratio:
                    return

        trade = Trade(
            trade_id=self._next_trade_id,
            symbol=sym,
            session=session_name,
            direction=signal.direction,
            entry_price=signal.ob_body_level,
            initial_sl=signal.sl_price,
            current_sl=signal.sl_price,
            tp_price=tp_price,
            risk_pct=risk_pct,
            risk_amount=risk_amount,
            size=size,
            placed_at=placed_at,
            expires_at=placed_at + timedelta(minutes=self.cfg.limit_expiry_minutes),
            is_news_trade=is_news,
            news_close_at=news_close_at,
            notes="momentum_5m" if signal.is_momentum else "1h_aoi",
        )
        self._next_trade_id += 1
        self.trades.append(trade)
        self._session_counts[(placed_at.date(), session_name)] += 1
        self._day_counts[placed_at.date()] += 1

    def _manage_symbol_trades(
        self,
        sym: str,
        ts: datetime,
        bar: pd.Series,
        window_5m: pd.DataFrame,
    ) -> None:
        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        bar_close = float(bar["close"])

        for trade in self.trades:
            if trade.symbol != sym:
                continue

            if trade.status == TradeStatus.PENDING:
                if ts >= trade.expires_at:
                    trade.status = TradeStatus.CANCELLED
                    trade.exit_reason = "EXPIRED"
                    trade.closed_at = ts
                    continue

                # Check limit fill
                if bar_low <= trade.entry_price <= bar_high:
                    trade.status = TradeStatus.OPEN
                    trade.filled_at = ts

            if trade.status == TradeStatus.OPEN:
                # 1. Forced close 1h after USD high-impact news if TP not hit
                if trade.is_news_trade and trade.news_close_at and ts >= trade.news_close_at:
                    self._close_trade(trade, bar_close, ts, "NEWS_WINDOW_END")
                    continue

                # 2. Check SL / TP (conservative: SL checked first if both hit in same bar)
                if trade.direction == Bias.BULLISH:
                    if bar_low <= trade.current_sl:
                        reason = "BE" if trade.moved_to_be else "SL"
                        self._close_trade(trade, trade.current_sl, ts, reason)
                        continue
                    if bar_high >= trade.tp_price:
                        self._close_trade(trade, trade.tp_price, ts, "TP")
                        continue
                else:
                    if bar_high >= trade.current_sl:
                        reason = "BE" if trade.moved_to_be else "SL"
                        self._close_trade(trade, trade.current_sl, ts, reason)
                        continue
                    if bar_low <= trade.tp_price:
                        self._close_trade(trade, trade.tp_price, ts, "TP")
                        continue

                # 3. 2-CISD Trade Management (BE & Early Cut) unless let_play_out=True
                if self.cfg.let_play_out:
                    continue
                opp_dir = Bias.BEARISH if trade.direction == Bias.BULLISH else Bias.BULLISH
                same_cisd = detect_5m_cisd(window_5m, trade.direction, swing_lookback=self.cfg.swing_lookback)
                opp_cisd = detect_5m_cisd(window_5m, opp_dir, swing_lookback=self.cfg.swing_lookback)

                if same_cisd is not None:
                    trade.same_dir_cisds += 1
                    trade.opp_consec_cisds = 0
                    rr_now = trade.current_rr(bar_close)
                    if (
                        not trade.moved_to_be
                        and trade.same_dir_cisds >= self.cfg.be_same_dir_cisds
                        and rr_now >= self.cfg.be_min_rr
                    ):
                        trade.current_sl = trade.entry_price
                        trade.moved_to_be = True

                elif opp_cisd is not None:
                    trade.opp_consec_cisds += 1
                    if trade.opp_consec_cisds >= self.cfg.cut_opp_consec_cisds:
                        rr_now = trade.current_rr(bar_close)
                        if rr_now > 0:
                            trade.current_sl = trade.entry_price
                            trade.moved_to_be = True
                        else:
                            self._close_trade(trade, bar_close, ts, "2_OPP_CISD_CUT")

    def _close_trade(self, trade: Trade, exit_price: float, ts: datetime, reason: str) -> None:
        trade.status = TradeStatus.CLOSED
        trade.exit_price = exit_price
        trade.closed_at = ts
        trade.exit_reason = reason

        if trade.direction == Bias.BULLISH:
            gross_pnl = (exit_price - trade.entry_price) * trade.size
        else:
            gross_pnl = (trade.entry_price - exit_price) * trade.size

        costs = 0.0
        if self.cfg.apply_costs:
            sc = SYMBOL_COSTS.get(trade.symbol)
            if sc is not None:
                spread_slip = (sc.spread + 2.0 * sc.slippage_per_side) * trade.size
                commission = 2.0 * sc.commission_rate * trade.entry_price * trade.size
                costs = spread_slip + commission

        trade.costs = costs
        trade.pnl = gross_pnl - costs
        trade.rr_realized = trade.pnl / trade.risk_amount if trade.risk_amount > 0 else 0.0
        self.equity += trade.pnl
