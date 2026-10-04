#!/usr/bin/env python3
"""Historical research for the VOO/QQQM opportunity-cash plan.

This module is intentionally separate from the real investment ledger and from
the existing allocation backtest.  It compares equal-cash controls with three
predeclared drawdown ladders using only validated VOO/QQQM adjusted daily close
data.  It never creates an order or changes a real transaction.

Signals are calculated after day ``t`` closes and execute at day ``t+1``'s
close.  The opportunity plan uses one combined four-stage ladder per market
cycle: the deeper of the VOO and QQQM stages is used, and a stage can execute
only once.  A new cycle starts only after both symbols have, since the cycle
began, separately closed above their previous 252 trading-day highs.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime
from html import escape
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


SYMBOLS = ("VOO", "QQQM")
TRADING_DAYS = 252
USE_RATIOS = (0.20, 0.25, 0.25, 0.30)
PROFILES = {
    "sensitive": {
        "label": "较敏感",
        "VOO": (0.05, 0.10, 0.15, 0.20),
        "QQQM": (0.08, 0.12, 0.18, 0.25),
    },
    "default": {
        "label": "V1 默认",
        "VOO": (0.08, 0.12, 0.18, 0.25),
        "QQQM": (0.10, 0.15, 0.22, 0.30),
    },
    "conservative": {
        "label": "更保守",
        "VOO": (0.10, 0.15, 0.20, 0.30),
        "QQQM": (0.12, 0.18, 0.25, 0.35),
    },
}


@dataclass(frozen=True)
class OpportunityConfig:
    """Explicit USD assumptions for one opportunity-strategy study.

    The real plan starts in October 2026.  Earlier rows remain useful as signal
    warm-up and as a fixed-DCA comparison, but opportunity cash is contributed
    only from ``opportunity_start_month`` onward.  No foreign-exchange rate is
    used anywhere in this research model.
    """

    monthly_dca_usd: float = 500.0
    opportunity_monthly_usd: float = 300.0
    opportunity_cap_usd: float = 1800.0
    opportunity_start_month: str = "2026-10"
    target_day: int = 10
    overflow_dca_months: int = 6
    voo_weight: float = 0.80
    qqqm_weight: float = 0.20

    def checked(self) -> "OpportunityConfig":
        numeric_positive = {
            "每月固定定投": self.monthly_dca_usd,
            "每月机会资金": self.opportunity_monthly_usd,
            "机会资金上限": self.opportunity_cap_usd,
        }
        for label, value in numeric_positive.items():
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value <= 0:
                raise ValueError(f"{label}必须是大于零的有限数字。")
        if type(self.target_day) is not int or not 1 <= self.target_day <= 28:
            raise ValueError("每月目标日必须是 1 至 28 的整数。")
        if type(self.overflow_dca_months) is not int or self.overflow_dca_months <= 0:
            raise ValueError("超额资金并入定投的月数必须是正整数。")
        try:
            period = pd.Period(self.opportunity_start_month, freq="M")
        except (TypeError, ValueError) as exc:
            raise ValueError("机会资金开始月份必须采用 YYYY-MM 格式。") from exc
        if str(period) != self.opportunity_start_month:
            raise ValueError("机会资金开始月份必须采用 YYYY-MM 格式。")
        weights = (self.voo_weight, self.qqqm_weight)
        if any(not isinstance(item, (int, float)) or not math.isfinite(float(item)) or item < 0
               for item in weights):
            raise ValueError("VOO/QQQM 权重必须是非负有限数字。")
        if not math.isclose(sum(weights), 1.0, rel_tol=0, abs_tol=1e-12):
            raise ValueError("VOO/QQQM 权重合计必须等于 1。")
        return self


def read_prices(path: Path) -> pd.DataFrame:
    """Read and strictly validate the common adjusted-close history."""
    try:
        prices = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
    except (OSError, ValueError, KeyError) as exc:
        raise ValueError(f"无法读取机会资金回测行情：{exc}") from exc
    return validate_prices(prices)


def validate_prices(prices: pd.DataFrame) -> pd.DataFrame:
    if not set(SYMBOLS).issubset(prices.columns):
        raise ValueError("行情必须同时包含 VOO 和 QQQM。")
    clean = prices.loc[:, list(SYMBOLS)].copy()
    try:
        clean.index = pd.DatetimeIndex(pd.to_datetime(clean.index)).tz_localize(None).normalize()
    except (TypeError, ValueError) as exc:
        raise ValueError("行情日期无法解析。") from exc
    clean = clean.sort_index()
    if clean.index.has_duplicates:
        raise ValueError("行情中存在重复交易日期。")
    clean = clean.apply(pd.to_numeric, errors="coerce")
    values = clean.to_numpy(dtype=float)
    if clean.isna().any().any() or not np.isfinite(values).all():
        raise ValueError("行情含缺失值或非有限数字；不会自动填充。")
    if (values <= 0).any():
        raise ValueError("行情价格必须全部大于零。")
    if len(clean) < TRADING_DAYS + 10:
        raise ValueError(f"共同交易日只有 {len(clean)} 天，至少需要 {TRADING_DAYS + 10} 天。")
    return clean


def _evaluation_start_position(prices: pd.DataFrame) -> int:
    """Use 252 prior rows as warm-up, then start at the next calendar month."""
    first_ready = prices.index[TRADING_DAYS]
    next_month = (first_ready + pd.offsets.MonthBegin(1)).normalize()
    position = int(prices.index.searchsorted(next_month, side="left"))
    if position >= len(prices) - 1:
        raise ValueError("完成 252 个交易日预热后，没有足够的评估行情。")
    return position


def _monthly_rows(index: pd.DatetimeIndex, target_day: int) -> set[pd.Timestamp]:
    rows: set[pd.Timestamp] = set()
    series = pd.Series(index, index=index)
    for _, month in series.groupby(index.to_period("M")):
        eligible = month[month.index.day >= target_day]
        # A truncated final month may end before the configured funding day.  Do
        # not move that future contribution earlier merely because the price file
        # stops; a complete month always has an eligible day when target_day <= 28.
        if len(eligible):
            rows.add(pd.Timestamp(eligible.iloc[0]))
    return rows


def _split_cents(amount: float, count: int) -> list[float]:
    cents = int(round(amount * 100))
    base, remainder = divmod(cents, count)
    return [(base + (1 if position < remainder else 0)) / 100 for position in range(count)]


def _consume_fifo(lots: list[dict], amount: float) -> list[dict]:
    """Remove ``amount`` USD from dated cash lots and return the consumed pieces."""
    remaining = round(float(amount), 2)
    consumed: list[dict] = []
    while remaining > 0.00001 and lots:
        lot = lots[0]
        take = min(remaining, lot["amount_usd"])
        take = round(take, 2)
        if take <= 0:
            lots.pop(0)
            continue
        consumed.append({"fund_date": lot["fund_date"], "amount_usd": take})
        lot["amount_usd"] = round(lot["amount_usd"] - take, 2)
        remaining = round(remaining - take, 2)
        if lot["amount_usd"] <= 0.00001:
            lots.pop(0)
    if remaining > 0.00001:
        raise RuntimeError("机会资金 FIFO 余额不足，模拟已停止。")
    return consumed


def _lot_total(lots: Iterable[dict]) -> float:
    return round(sum(float(item["amount_usd"]) for item in lots), 2)


def _opportunity_active(timestamp: pd.Timestamp, config: OpportunityConfig) -> bool:
    """Return whether the USD opportunity contribution plan is active."""
    return timestamp.to_period("M") >= pd.Period(config.opportunity_start_month, freq="M")


def _stage(drawdown: float, thresholds: tuple[float, ...]) -> int:
    return sum(drawdown + 1e-15 >= threshold for threshold in thresholds)


def _buy(shares: np.ndarray, amount_usd: float, row: pd.Series,
         weights: np.ndarray) -> None:
    if amount_usd <= 0:
        return
    shares += amount_usd * weights / row.to_numpy(dtype=float)


def _xirr(flows: pd.Series, terminal: float, terminal_date: pd.Timestamp) -> float:
    positive = flows[flows > 0]
    if positive.empty or terminal < 0:
        return math.nan
    d0 = positive.index[0]
    years = np.array([(item - d0).days / 365.25 for item in positive.index]
                     + [(terminal_date - d0).days / 365.25], dtype=float)
    amounts = np.array([-float(item) for item in positive.to_numpy()] + [float(terminal)])

    def npv(rate: float) -> float:
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            return float(np.sum(amounts / np.power(1.0 + rate, years)))

    lower, upper = -0.9999, 100.0
    low_value, high_value = npv(lower), npv(upper)
    if not math.isfinite(low_value) or not math.isfinite(high_value) or low_value * high_value > 0:
        return math.nan
    for _ in range(240):
        middle = (lower + upper) / 2
        value = npv(middle)
        if not math.isfinite(value):
            lower = middle
            continue
        if abs(value) < 1e-10:
            return middle
        if low_value * value <= 0:
            upper, high_value = middle, value
        else:
            lower, low_value = middle, value
    return (lower + upper) / 2


def _drawdown_episode(nav: pd.Series) -> dict:
    drawdown = nav / nav.cummax() - 1
    trough = drawdown.idxmin()
    peak = nav.loc[:trough].idxmax()
    peak_value = float(nav.loc[peak])
    recovered = nav.loc[trough:][nav.loc[trough:] >= peak_value]
    recovery = recovered.index[0] if len(recovered) else None
    end = recovery if recovery is not None else nav.index[-1]
    return {
        "max_drawdown": float(drawdown.min()),
        "peak": peak.strftime("%Y-%m-%d"),
        "trough": trough.strftime("%Y-%m-%d"),
        "recovery": recovery.strftime("%Y-%m-%d") if recovery is not None else None,
        "days_underwater": int((end - peak).days),
    }


def _base_metrics(strategy: str, label: str, daily: pd.DataFrame) -> dict:
    account = daily["account_value_usd"]
    deposits = daily["external_flow_usd"]
    returns = daily["twr_return"]
    nav = daily["twr_nav"]
    funded = account[account > 0]
    years = ((daily.index[-1] - funded.index[0]).days / 365.25
             if len(funded) and daily.index[-1] > funded.index[0] else math.nan)
    twr_total = float(nav.iloc[-1] - 1)
    cagr = (float(nav.iloc[-1]) ** (1 / years) - 1
            if years and math.isfinite(years) and years > 0 and nav.iloc[-1] > 0 else math.nan)
    episode = _drawdown_episode(nav)
    volatility = float(returns.std(ddof=1) * math.sqrt(TRADING_DAYS)) if len(returns) > 1 else math.nan
    metrics = {
        "strategy": strategy,
        "label": label,
        "start": daily.index[0].strftime("%Y-%m-%d"),
        "end": daily.index[-1].strftime("%Y-%m-%d"),
        "contributed_usd": float(deposits.sum()),
        "ending_usd": float(account.iloc[-1]),
        "profit_usd": float(account.iloc[-1] - deposits.sum()),
        "money_weighted_xirr": _xirr(deposits, float(account.iloc[-1]), daily.index[-1]),
        "time_weighted_total_return": twr_total,
        "time_weighted_cagr": cagr,
        "annualized_volatility": volatility,
        "cagr_to_volatility": cagr / volatility if volatility and volatility > 0 else math.nan,
        "max_twr_drawdown": episode["max_drawdown"],
        "max_dd_peak_date": episode["peak"],
        "max_dd_trough_date": episode["trough"],
        "max_dd_recovery_date": episode["recovery"],
        "max_dd_days_underwater": episode["days_underwater"],
        "num_external_contributions": int((deposits > 0).sum()),
    }
    for period in (1, 3, 5):
        rolling = (nav / nav.shift(TRADING_DAYS * period) - 1).dropna()
        metrics[f"rolling_{period}y_worst"] = float(rolling.min()) if len(rolling) else math.nan
        metrics[f"rolling_{period}y_best"] = float(rolling.max()) if len(rolling) else math.nan
        metrics[f"rolling_{period}y_samples"] = int(len(rolling))
    return metrics


def _finish_daily(rows: list[dict]) -> pd.DataFrame:
    daily = pd.DataFrame(rows).set_index("date")
    daily.index = pd.DatetimeIndex(daily.index)
    previous = daily["account_value_usd"].shift(1)
    returns = pd.Series(0.0, index=daily.index)
    funded = previous > 0
    returns.loc[funded] = ((daily.loc[funded, "account_value_usd"]
                            - daily.loc[funded, "external_flow_usd"])
                           / previous.loc[funded] - 1)
    daily["twr_return"] = returns
    daily["twr_nav"] = (1 + returns).cumprod()
    daily["twr_drawdown"] = daily["twr_nav"] / daily["twr_nav"].cummax() - 1
    daily["cumulative_contributions_usd"] = daily["external_flow_usd"].cumsum()
    return daily


def _simulate_control(prices: pd.DataFrame, monthly_dates: set[pd.Timestamp],
                      config: OpportunityConfig, matched: bool) -> tuple[pd.DataFrame, dict]:
    weights = np.array((config.voo_weight, config.qqqm_weight), dtype=float)
    shares = np.zeros(2, dtype=float)
    rows: list[dict] = []
    opportunity_contributed = 0.0
    for timestamp, row in prices.iterrows():
        external = 0.0
        fixed_buy = opportunity_buy = 0.0
        if timestamp in monthly_dates:
            opportunity_usd = (config.opportunity_monthly_usd
                               if matched and _opportunity_active(timestamp, config) else 0.0)
            external = config.monthly_dca_usd + opportunity_usd
            fixed_buy = config.monthly_dca_usd
            opportunity_buy = opportunity_usd
            opportunity_contributed += opportunity_usd
            _buy(shares, external, row, weights)
        market_value = float(np.dot(shares, row.to_numpy(dtype=float)))
        rows.append({
            "date": timestamp,
            "external_flow_usd": external,
            "fixed_dca_buy_usd": fixed_buy,
            "opportunity_trigger_buy_usd": 0.0,
            "overflow_dca_buy_usd": opportunity_buy,
            "voo_shares": shares[0],
            "qqqm_shares": shares[1],
            "market_value_usd": market_value,
            "opportunity_pool_usd": 0.0,
            "reserved_trigger_usd": 0.0,
            "overflow_pending_usd": 0.0,
            "uninvested_opportunity_usd": 0.0,
            "cash_value_usd": 0.0,
            "account_value_usd": market_value,
        })
    daily = _finish_daily(rows)
    key = "A_matched" if matched else "A_fixed"
    label = "A｜等资金每月投入" if matched else "A｜仅固定定投"
    metrics = _base_metrics(key, label, daily)
    metrics.update({
        "profile": "matched" if matched else "fixed_only",
        "opportunity_contributed_usd": opportunity_contributed,
        "trigger_deployed_usd": 0.0,
        "overflow_deployed_usd": opportunity_contributed,
        "trigger_utilization_pct": 0.0,
        "total_opportunity_invested_pct": (
            100.0 if matched and opportunity_contributed > 0
            else 0.0 if matched else math.nan
        ),
        "average_uninvested_usd": 0.0,
        "maximum_uninvested_usd": 0.0,
        "average_deployed_wait_days": 0.0,
        "ending_cash_weighted_age_days": 0.0,
        "trigger_count": 0,
        "cycle_reset_count": 0,
        "stage_1_count": 0,
        "stage_2_count": 0,
        "stage_3_count": 0,
        "stage_4_count": 0,
        "post_6m_average_return": math.nan,
        "post_6m_samples": 0,
        "post_12m_average_return": math.nan,
        "post_12m_samples": 0,
    })
    return daily, metrics


def _post_event_returns(events: list[dict], prices: pd.DataFrame,
                        weights: np.ndarray) -> None:
    for event in events:
        if event.get("event_type") != "trigger_buy" or event.get("status") != "executed":
            continue
        execution_date = pd.Timestamp(event["execution_date"])
        entry = prices.loc[execution_date].to_numpy(dtype=float)
        for months in (6, 12):
            target = execution_date + pd.DateOffset(months=months)
            position = int(prices.index.searchsorted(target, side="left"))
            key = f"post_{months}m_return"
            date_key = f"post_{months}m_date"
            if position >= len(prices):
                event[key] = math.nan
                event[date_key] = None
            else:
                future = prices.iloc[position].to_numpy(dtype=float)
                event[key] = float(np.dot(weights, future / entry - 1))
                event[date_key] = prices.index[position].strftime("%Y-%m-%d")


def _simulate_profile(full_prices: pd.DataFrame, evaluation_position: int,
                      monthly_dates: set[pd.Timestamp], config: OpportunityConfig,
                      profile_name: str) -> tuple[pd.DataFrame, dict, list[dict]]:
    profile = PROFILES[profile_name]
    weights = np.array((config.voo_weight, config.qqqm_weight), dtype=float)
    shares = np.zeros(2, dtype=float)
    pool_lots: list[dict] = []
    overflow_queue: list[dict] = []
    pending: list[dict] = []
    events: list[dict] = []
    rows: list[dict] = []
    global_stage = 0
    cycle_id = 1
    cycle_active = False
    high_flags = {symbol: False for symbol in SYMBOLS}
    reset_count = 0
    trigger_deployed = 0.0
    overflow_deployed = 0.0
    weighted_wait_usd_days = 0.0
    opportunity_contributed = 0.0

    for position in range(evaluation_position, len(full_prices)):
        timestamp = full_prices.index[position]
        row = full_prices.iloc[position]
        external = 0.0
        fixed_buy = 0.0
        trigger_buy = 0.0
        overflow_buy = 0.0

        # Signals seen at the previous close execute now.  Their cash was
        # reserved on the signal date, so today's new contribution cannot make
        # an old signal larger.
        if pending:
            for event in pending:
                amount_usd = event["amount_usd"]
                _buy(shares, amount_usd, row, weights)
                trigger_buy += amount_usd
                trigger_deployed += amount_usd
                event["execution_date"] = timestamp.strftime("%Y-%m-%d")
                event["execution_price_voo"] = float(row["VOO"])
                event["execution_price_qqqm"] = float(row["QQQM"])
                event["voo_amount_usd"] = amount_usd * config.voo_weight
                event["qqqm_amount_usd"] = amount_usd * config.qqqm_weight
                event["status"] = "executed"
                wait_value = sum(piece["amount_usd"]
                                 * (timestamp.date() - piece["fund_date"]).days
                                 for piece in event.pop("_lot_parts"))
                event["weighted_wait_usd_days"] = wait_value
                event["average_wait_days"] = wait_value / amount_usd if amount_usd else 0.0
                weighted_wait_usd_days += wait_value
            pending = []

        if timestamp in monthly_dates:
            monthly_opportunity = (config.opportunity_monthly_usd
                                   if _opportunity_active(timestamp, config) else 0.0)
            external = config.monthly_dca_usd + monthly_opportunity
            fixed_buy = config.monthly_dca_usd
            _buy(shares, fixed_buy, row, weights)

            # Previously scheduled excess is invested before this month's new
            # excess is scheduled; all actions use the same known close.
            current_period = timestamp.to_period("M")
            due = [item for item in overflow_queue if item["due_period"] <= current_period]
            overflow_queue = [item for item in overflow_queue if item["due_period"] > current_period]
            for item in due:
                amount_usd = item["amount_usd"]
                _buy(shares, amount_usd, row, weights)
                overflow_buy += amount_usd
                overflow_deployed += amount_usd
                wait_days = (timestamp.date() - item["fund_date"]).days
                weighted_wait_usd_days += amount_usd * wait_days
                events.append({
                    "strategy": f"B_{profile_name}",
                    "profile": profile_name,
                    "event_type": "overflow_dca",
                    "status": "executed",
                    "signal_date": None,
                    "execution_date": timestamp.strftime("%Y-%m-%d"),
                    "cycle_id": None,
                    "trigger_symbol": None,
                    "stage": None,
                    "use_ratio": None,
                    "voo_drawdown": None,
                    "qqqm_drawdown": None,
                    "amount_usd": amount_usd,
                    "voo_amount_usd": amount_usd * config.voo_weight,
                    "qqqm_amount_usd": amount_usd * config.qqqm_weight,
                    "execution_price_voo": float(row["VOO"]),
                    "execution_price_qqqm": float(row["QQQM"]),
                    "fund_date": item["fund_date"].isoformat(),
                    "average_wait_days": wait_days,
                    "weighted_wait_usd_days": amount_usd * wait_days,
                })

            opportunity_contributed += monthly_opportunity
            pool_before = _lot_total(pool_lots)
            room = max(0.0, round(config.opportunity_cap_usd - pool_before, 2))
            retained = min(monthly_opportunity, room)
            overflow = round(monthly_opportunity - retained, 2)
            if retained > 0:
                pool_lots.append({"fund_date": timestamp.date(), "amount_usd": round(retained, 2)})
            if overflow > 0:
                pieces = _split_cents(overflow, config.overflow_dca_months)
                for offset, piece in enumerate(pieces, start=1):
                    scheduled = {
                        "due_period": current_period + offset,
                        "amount_usd": piece,
                        "fund_date": timestamp.date(),
                    }
                    overflow_queue.append(scheduled)
                    events.append({
                        "strategy": f"B_{profile_name}",
                        "profile": profile_name,
                        "event_type": "overflow_scheduled",
                        "status": "scheduled",
                        "signal_date": timestamp.strftime("%Y-%m-%d"),
                        "execution_date": None,
                        "cycle_id": None,
                        "trigger_symbol": None,
                        "stage": None,
                        "use_ratio": None,
                        "voo_drawdown": None,
                        "qqqm_drawdown": None,
                        "amount_usd": piece,
                        "voo_amount_usd": None,
                        "qqqm_amount_usd": None,
                        "execution_price_voo": None,
                        "execution_price_qqqm": None,
                        "fund_date": timestamp.strftime("%Y-%m-%d"),
                        "due_month": str(current_period + offset),
                        "average_wait_days": None,
                        "weighted_wait_usd_days": None,
                    })

        prior = full_prices.iloc[position - TRADING_DAYS:position]
        prior_highs = prior.max()
        strict_high = {
            symbol: float(row[symbol]) > float(prior_highs[symbol])
            for symbol in SYMBOLS
        }
        if cycle_active:
            for symbol in SYMBOLS:
                high_flags[symbol] = high_flags[symbol] or strict_high[symbol]
            if all(high_flags.values()):
                global_stage = 0
                cycle_active = False
                high_flags = {symbol: False for symbol in SYMBOLS}
                cycle_id += 1
                reset_count += 1

        rolling = full_prices.iloc[position - TRADING_DAYS + 1:position + 1]
        highs = rolling.max()
        drawdowns = {
            symbol: max(0.0, 1.0 - float(row[symbol]) / float(highs[symbol]))
            for symbol in SYMBOLS
        }
        stages = {
            symbol: _stage(drawdowns[symbol], tuple(profile[symbol]))
            for symbol in SYMBOLS
        }
        combined_stage = max(stages.values())
        if combined_stage > global_stage:
            if not cycle_active:
                cycle_active = True
                high_flags = {symbol: False for symbol in SYMBOLS}
            for stage in range(global_stage + 1, combined_stage + 1):
                available = _lot_total(pool_lots)
                amount_usd = round(available * USE_RATIOS[stage - 1], 2)
                if amount_usd <= 0:
                    break
                lot_parts = _consume_fifo(pool_lots, amount_usd)
                trigger_symbols = [symbol for symbol in SYMBOLS if stages[symbol] == combined_stage]
                event = {
                    "strategy": f"B_{profile_name}",
                    "profile": profile_name,
                    "event_type": "trigger_buy",
                    "status": "pending",
                    "signal_date": timestamp.strftime("%Y-%m-%d"),
                    "execution_date": None,
                    "cycle_id": cycle_id,
                    "trigger_symbol": "+".join(trigger_symbols),
                    "stage": stage,
                    "use_ratio": USE_RATIOS[stage - 1],
                    "voo_drawdown": drawdowns["VOO"],
                    "qqqm_drawdown": drawdowns["QQQM"],
                    "pool_before_usd": available,
                    "amount_usd": amount_usd,
                    "voo_amount_usd": None,
                    "qqqm_amount_usd": None,
                    "execution_price_voo": None,
                    "execution_price_qqqm": None,
                    "fund_date": None,
                    "average_wait_days": None,
                    "weighted_wait_usd_days": None,
                    "_lot_parts": lot_parts,
                }
                events.append(event)
                pending.append(event)
                global_stage = stage

        pool_usd = _lot_total(pool_lots)
        reserved_usd = round(sum(event["amount_usd"] for event in pending), 2)
        overflow_usd = round(sum(item["amount_usd"] for item in overflow_queue), 2)
        uninvested_usd = round(pool_usd + reserved_usd + overflow_usd, 2)
        market_value = float(np.dot(shares, row.to_numpy(dtype=float)))
        cash_value = uninvested_usd
        rows.append({
            "date": timestamp,
            "external_flow_usd": external,
            "fixed_dca_buy_usd": fixed_buy,
            "opportunity_trigger_buy_usd": trigger_buy,
            "overflow_dca_buy_usd": overflow_buy,
            "voo_shares": shares[0],
            "qqqm_shares": shares[1],
            "market_value_usd": market_value,
            "opportunity_pool_usd": pool_usd,
            "reserved_trigger_usd": reserved_usd,
            "overflow_pending_usd": overflow_usd,
            "uninvested_opportunity_usd": uninvested_usd,
            "cash_value_usd": cash_value,
            "account_value_usd": market_value + cash_value,
            "combined_stage": global_stage,
            "cycle_id": cycle_id,
        })

    daily = _finish_daily(rows)
    _post_event_returns(events, full_prices.loc[daily.index[0]:], weights)
    executed = [item for item in events
                if item["event_type"] == "trigger_buy" and item["status"] == "executed"]
    deployed_total = trigger_deployed + overflow_deployed
    end_date = daily.index[-1].date()
    remaining_pieces = list(pool_lots)
    for event in pending:
        remaining_pieces.extend(event.get("_lot_parts", []))
    remaining_pieces.extend({"fund_date": item["fund_date"], "amount_usd": item["amount_usd"]}
                            for item in overflow_queue)
    end_cash = _lot_total(remaining_pieces)
    end_weighted_age = sum(item["amount_usd"] * (end_date - item["fund_date"]).days
                           for item in remaining_pieces)
    post6 = [item["post_6m_return"] for item in executed
             if math.isfinite(float(item.get("post_6m_return", math.nan)))]
    post12 = [item["post_12m_return"] for item in executed
              if math.isfinite(float(item.get("post_12m_return", math.nan)))]
    key = f"B_{profile_name}"
    metrics = _base_metrics(key, f"B｜{profile['label']}档位", daily)
    metrics.update({
        "profile": profile_name,
        "opportunity_contributed_usd": opportunity_contributed,
        "trigger_deployed_usd": trigger_deployed,
        "overflow_deployed_usd": overflow_deployed,
        "trigger_utilization_pct": (trigger_deployed / opportunity_contributed * 100
                                    if opportunity_contributed else 0.0),
        "total_opportunity_invested_pct": (deployed_total / opportunity_contributed * 100
                                           if opportunity_contributed else 0.0),
        "average_uninvested_usd": float(daily["uninvested_opportunity_usd"].mean()),
        "maximum_uninvested_usd": float(daily["uninvested_opportunity_usd"].max()),
        "average_deployed_wait_days": (weighted_wait_usd_days / deployed_total
                                       if deployed_total else 0.0),
        "ending_cash_weighted_age_days": end_weighted_age / end_cash if end_cash else 0.0,
        "ending_uninvested_usd": end_cash,
        "trigger_count": len(executed),
        "cycle_reset_count": reset_count,
        **{f"stage_{stage}_count": sum(item["stage"] == stage for item in executed)
           for stage in range(1, 5)},
        "post_6m_average_return": float(np.mean(post6)) if post6 else math.nan,
        "post_6m_samples": len(post6),
        "post_12m_average_return": float(np.mean(post12)) if post12 else math.nan,
        "post_12m_samples": len(post12),
    })
    for event in events:
        event.pop("_lot_parts", None)
    return daily, metrics, events


def simulate_strategies(
    prices: pd.DataFrame, config: OpportunityConfig,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame], pd.DataFrame]:
    """Return summary, daily frames and a complete opportunity-event ledger."""
    config = config.checked()
    full = validate_prices(prices)
    start_position = _evaluation_start_position(full)
    evaluation = full.iloc[start_position:].copy()
    monthly_dates = _monthly_rows(evaluation.index, config.target_day)
    if not monthly_dates:
        raise ValueError("评估区间没有可用的月度投入日。")

    daily_frames: dict[str, pd.DataFrame] = {}
    summaries: list[dict] = []
    all_events: list[dict] = []
    for matched in (False, True):
        daily, metrics = _simulate_control(evaluation, monthly_dates, config, matched)
        daily_frames[metrics["strategy"]] = daily
        summaries.append(metrics)
    for profile_name in PROFILES:
        daily, metrics, events = _simulate_profile(
            full, start_position, monthly_dates, config, profile_name)
        daily_frames[metrics["strategy"]] = daily
        summaries.append(metrics)
        all_events.extend(events)

    summary = pd.DataFrame(summaries)
    matched = summary.loc[summary["strategy"] == "A_matched"].iloc[0]
    for index, row in summary.iterrows():
        if row["strategy"].startswith("B_"):
            summary.loc[index, "ending_vs_matched_usd"] = row["ending_usd"] - matched["ending_usd"]
            summary.loc[index, "xirr_vs_matched_pp"] = (
                (row["money_weighted_xirr"] - matched["money_weighted_xirr"]) * 100)
            summary.loc[index, "max_drawdown_vs_matched_pp"] = (
                (row["max_twr_drawdown"] - matched["max_twr_drawdown"]) * 100)
    events_frame = pd.DataFrame(all_events)
    return summary, daily_frames, events_frame


def _fmt_money(value: object) -> str:
    return "—" if pd.isna(value) else f"${float(value):,.0f}"


def _fmt_pct(value: object, *, points: bool = False) -> str:
    if pd.isna(value):
        return "数据不足"
    amount = float(value) if points else float(value) * 100
    return f"{amount:+.2f}%"


def _svg_chart(daily_frames: dict[str, pd.DataFrame], column: str,
               *, percent: bool = False) -> str:
    width, height, pad = 920, 330, 45
    series = {name: frame[column].to_numpy(dtype=float) for name, frame in daily_frames.items()}
    all_values = np.concatenate(list(series.values()))
    low, high = float(np.nanmin(all_values)), float(np.nanmax(all_values))
    if math.isclose(low, high):
        high = low + 1
    colors = {
        "A_fixed": "#81938c", "A_matched": "#173f37", "B_sensitive": "#d18b36",
        "B_default": "#2869b2", "B_conservative": "#8a5aa6",
    }
    lines = []
    for name, values in series.items():
        if len(values) == 1:
            xs = [pad]
        else:
            xs = np.linspace(pad, width - pad, len(values))
        ys = height - pad - (values - low) / (high - low) * (height - 2 * pad)
        points = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
        lines.append(f'<polyline points="{points}" fill="none" stroke="{colors[name]}" stroke-width="2"/>')
    formatter = (lambda item: f"{item * 100:.1f}%") if percent else (lambda item: f"${item:,.0f}")
    legend = "".join(
        f'<span><i style="background:{colors[name]}"></i>{escape(name.replace("_", " "))}</span>'
        for name in series
    )
    return (f'<div class="legend">{legend}</div><svg viewBox="0 0 {width} {height}" role="img">'
            f'<line x1="{pad}" y1="{pad}" x2="{pad}" y2="{height-pad}" stroke="#aab7b1"/>'
            f'<line x1="{pad}" y1="{height-pad}" x2="{width-pad}" y2="{height-pad}" stroke="#aab7b1"/>'
            f'<text x="4" y="{pad+4}" font-size="11">{escape(formatter(high))}</text>'
            f'<text x="4" y="{height-pad+4}" font-size="11">{escape(formatter(low))}</text>'
            f'{"".join(lines)}</svg>')


def render_report(out_dir: Path, prices: pd.DataFrame, summary: pd.DataFrame,
                  daily_frames: dict[str, pd.DataFrame], events: pd.DataFrame,
                  config: OpportunityConfig, source_label: str) -> Path:
    end = prices.index[-1].strftime("%Y-%m-%d")
    start = next(iter(daily_frames.values())).index[0].strftime("%Y-%m-%d")
    rows = []
    for _, item in summary.iterrows():
        values = [
            escape(str(item["label"])), _fmt_money(item["contributed_usd"]),
            _fmt_money(item["ending_usd"]), _fmt_money(item["profit_usd"]),
            _fmt_pct(item["money_weighted_xirr"]), _fmt_pct(item["time_weighted_cagr"]),
            _fmt_pct(item["annualized_volatility"]), _fmt_pct(item["max_twr_drawdown"]),
        ]
        rows.append("<tr>" + "".join(f"<td>{value}</td>" for value in values) + "</tr>")
    efficiency_rows = []
    for _, item in summary[summary["strategy"].str.startswith("B_")].iterrows():
        values = [
            escape(str(item["label"])), str(int(item["trigger_count"])),
            _fmt_money(item["average_uninvested_usd"]),
            _fmt_money(item["maximum_uninvested_usd"]),
            f'{item["trigger_utilization_pct"]:.1f}%', f'{item["total_opportunity_invested_pct"]:.1f}%',
            f'{item["average_deployed_wait_days"]:.1f} 天',
            f'{item["ending_cash_weighted_age_days"]:.1f} 天',
            _fmt_pct(item["post_6m_average_return"]), str(int(item["post_6m_samples"])),
            _fmt_pct(item["post_12m_average_return"]), str(int(item["post_12m_samples"])),
        ]
        efficiency_rows.append("<tr>" + "".join(f"<td>{value}</td>" for value in values) + "</tr>")
    event_rows = []
    if not events.empty:
        trigger_events = events[(events["event_type"] == "trigger_buy")
                                & (events["status"] == "executed")]
        for _, item in trigger_events.iterrows():
            event_rows.append("<tr>" + "".join(f"<td>{escape(str(value))}</td>" for value in [
                PROFILES[str(item["profile"])]["label"], item["signal_date"], item["execution_date"],
                int(item["cycle_id"]), item["trigger_symbol"], int(item["stage"]),
                f'{float(item["voo_drawdown"])*100:.1f}%', f'{float(item["qqqm_drawdown"])*100:.1f}%',
                _fmt_money(item["amount_usd"]), f'{float(item["average_wait_days"]):.1f} 天',
            ]) + "</tr>")
    if not event_rows:
        event_rows.append('<tr><td colspan="10">本区间没有已执行的机会档位。</td></tr>')
    account_chart = _svg_chart(daily_frames, "account_value_usd")
    drawdown_chart = _svg_chart(daily_frames, "twr_drawdown", percent=True)
    parameters = escape(json.dumps(asdict(config), ensure_ascii=False))
    html = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>VOO / QQQM 机会资金回测｜{end}</title>
<style>*{{box-sizing:border-box}}body{{margin:0;background:#f3f6f4;color:#1d312b;font:14px/1.7 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}}main{{max-width:1180px;margin:auto;padding:25px 15px 55px}}header{{padding:27px;border-radius:17px;background:#173f37;color:white}}h1{{margin:0 0 5px;font-size:27px}}section{{background:white;border:1px solid #dfe8e3;border-radius:15px;padding:21px;margin-top:16px}}h2{{margin:0 0 11px;font-size:19px}}.note{{padding:13px;border-left:4px solid #c98e39;background:#fff8ea}}.ok{{padding:13px;border-left:4px solid #4f9366;background:#eff8f1}}.muted{{color:#6f8079}}.table{{overflow:auto}}table{{border-collapse:collapse;width:100%;min-width:880px}}th,td{{padding:9px 8px;border-bottom:1px solid #e5ece8;text-align:right;white-space:nowrap}}th{{background:#eff5f1}}td:first-child,th:first-child{{text-align:left}}svg{{width:100%;height:auto;background:#fbfcfb;border:1px solid #e2e9e5;border-radius:10px}}.legend{{display:flex;flex-wrap:wrap;gap:13px;margin:8px 0}}.legend i{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px}}code{{background:#eef2f0;padding:2px 4px;border-radius:4px}}@media(max-width:650px){{main{{padding:10px}}section{{padding:14px}}h1{{font-size:22px}}}}</style></head>
<body><main><header><h1>VOO / QQQM 机会资金历史回测</h1><div>评估区间 {start} 至 {end}｜先使用 252 个共同交易日预热｜行情为复权日收盘价</div></header>
<section><h2>研究范围</h2><p class="ok">主要比较对象是“A｜等资金每月投入”和三套 B 档位。它们每月获得完全相同的外部资金，因此期末资产、XIRR 和回撤差异才具有可比性。“A｜仅固定定投”只保留为实际固定计划的参考。</p><p class="note"><strong>Strategy C 暂不可回测：</strong>现有文件没有历史逐日的 2Y、10Y、VIX、盈利预期和流动性数据。使用今天的市场环境快照替代历史点时数据会造成未来数据泄漏。</p></section>
<section><h2>收益与风险</h2><div class="table"><table><thead><tr><th>策略</th><th>累计外部本金</th><th>期末资产</th><th>盈利</th><th>XIRR</th><th>TWR 年化</th><th>年化波动率</th><th>最大回撤</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>
<section><h2>账户总资产</h2>{account_chart}</section><section><h2>时间加权净值回撤</h2>{drawdown_chart}</section>
<section><h2>机会资金效率</h2><div class="table"><table><thead><tr><th>策略</th><th>档位成交</th><th>日均闲置</th><th>最高闲置</th><th>触发使用率</th><th>总投资率</th><th>已投入平均等待</th><th>期末现金平均年龄</th><th>买后6月</th><th>样本</th><th>买后12月</th><th>样本</th></tr></thead><tbody>{''.join(efficiency_rows)}</tbody></table></div></section>
<section><h2>机会档位明细</h2><div class="table"><table><thead><tr><th>档位版本</th><th>信号日</th><th>成交日</th><th>周期</th><th>触发标的</th><th>档位</th><th>VOO回撤</th><th>QQQM回撤</th><th>投入</th><th>资金等待</th></tr></thead><tbody>{''.join(event_rows)}</tbody></table></div></section>
<section><h2>统一模拟口径</h2><ul><li>所有资金统一以美元计量。固定定投研究金额为每月 ${config.monthly_dca_usd:,.2f}；机会资金从 {config.opportunity_start_month} 起每月增加 ${config.opportunity_monthly_usd:,.2f}，常备上限 ${config.opportunity_cap_usd:,.2f}。</li><li>固定定投研究金额在整个评估区间保持不变；机会资金开始月份以前不虚构额外入金。未来行情覆盖开始月份后，机会资金计划自动进入模拟。</li><li>每月 {config.target_day} 日当天或之后的首个共同交易日入金；超出上限的部分从下月起分 {config.overflow_dca_months} 个月并入普通定投。</li><li>组合档位取 VOO 与 QQQM 各自档位的较高者；同一周期每档只执行一次。两个标的都分别严格创出新的 252 日收盘高点后才重置。</li><li>日 t 收盘确认信号，预留当时已有的机会现金，日 t+1 收盘成交。每笔按 VOO {config.voo_weight*100:.0f}% / QQQM {config.qqqm_weight*100:.0f}% 买入，允许碎股，不卖出再平衡。</li><li>机会现金按 0% 收益；不计税费、手续费、价差和跨境成本。复权价格反映分红和拆分，不重复计入分红。</li><li>数据来源：{escape(source_label)}。</li></ul><p class="muted">运行参数：<code>{parameters}</code></p></section>
<section><h2>解释限制</h2><p>三套阈值均来自事先给定的规划，本报告并列展示，不从同一段历史挑选“最佳参数”后再宣称有效。QQQM 的共同历史较短，完成预热后的样本约五年，触发次数和市场周期数量都有限；结果用于检查规则行为和机会成本，不预测未来收益。</p></section>
</main></body></html>'''
    path = out_dir / f"opportunity_strategy_report_{end}.html"
    path.write_text(html, encoding="utf-8")
    return path


def run_opportunity_backtest(price_csv: Path, out_dir: Path, *,
                             monthly_dca_usd: float = 500.0,
                             opportunity_monthly_usd: float = 300.0,
                             opportunity_cap_usd: float = 1800.0,
                             opportunity_start_month: str | None = None,
                             target_day: int = 10, overflow_dca_months: int = 6,
                             voo_weight: float = 0.80, qqqm_weight: float = 0.20,
                             source_label: str | None = None,
                             **legacy_kwargs: float) -> Path:
    """Run the study, write auditable CSV/JSON files, and return the HTML path."""
    # Keep older in-process callers working during the USD migration.  Legacy
    # inputs are translated at the boundary and never appear in saved outputs.
    legacy_rate = legacy_kwargs.pop("cny_per_usd", None)
    legacy_monthly = legacy_kwargs.pop("opportunity_monthly_cny", None)
    legacy_cap = legacy_kwargs.pop("opportunity_cap_cny", None)
    if legacy_kwargs:
        unknown = ", ".join(sorted(legacy_kwargs))
        raise TypeError(f"不支持的机会回测参数：{unknown}")
    legacy_mode = any(value is not None for value in (legacy_rate, legacy_monthly, legacy_cap))
    if legacy_monthly is not None or legacy_cap is not None:
        if (legacy_rate is None or not isinstance(legacy_rate, (int, float))
                or not math.isfinite(float(legacy_rate)) or float(legacy_rate) <= 0):
            raise ValueError("旧版机会资金参数需要有效的换算率才能迁移为美元。")
        if legacy_monthly is not None:
            opportunity_monthly_usd = float(legacy_monthly) / float(legacy_rate)
        if legacy_cap is not None:
            opportunity_cap_usd = float(legacy_cap) / float(legacy_rate)
    start_month = opportunity_start_month or ("1900-01" if legacy_mode else "2026-10")
    config = OpportunityConfig(
        monthly_dca_usd=monthly_dca_usd,
        opportunity_monthly_usd=opportunity_monthly_usd,
        opportunity_cap_usd=opportunity_cap_usd,
        opportunity_start_month=start_month,
        target_day=target_day,
        overflow_dca_months=overflow_dca_months,
        voo_weight=voo_weight,
        qqqm_weight=qqqm_weight,
    ).checked()
    prices = read_prices(Path(price_csv))
    summary, daily_frames, events = simulate_strategies(prices, config)
    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    prices.to_csv(output / "opportunity_source_adjusted_close.csv", index_label="Date")
    summary.to_csv(output / "opportunity_strategy_summary.csv", index=False)
    events.to_csv(output / "opportunity_events.csv", index=False)
    for strategy, frame in daily_frames.items():
        frame.to_csv(output / f"opportunity_daily_{strategy}.csv", index_label="Date")
    parameters = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "price_start": prices.index[0].strftime("%Y-%m-%d"),
        "price_end": prices.index[-1].strftime("%Y-%m-%d"),
        "evaluation_start": next(iter(daily_frames.values())).index[0].strftime("%Y-%m-%d"),
        "source_label": source_label or "本地 VOO/QQQM 自动复权日收盘价 CSV；本次仅离线研究",
        "config": asdict(config),
        "profiles": PROFILES,
        "use_ratios": USE_RATIOS,
        "warmup_trading_days": TRADING_DAYS,
        "signal_execution_lag_trading_days": 1,
        "trigger_mode": "combined_max_stage",
        "cycle_reset": "both_symbols_strict_new_prior_252_day_close_high",
        "strategy_c_status": "unavailable_without_historical_point_in_time_data",
    }
    (output / "opportunity_parameters.json").write_text(
        json.dumps(parameters, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return render_report(
        output, prices, summary, daily_frames, events, config,
        source_label or "本地 VOO/QQQM 自动复权日收盘价 CSV；本次仅离线研究",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="VOO/QQQM 机会资金历史回测")
    parser.add_argument("--prices", required=True, help="含 Date、VOO、QQQM 的复权收盘价 CSV")
    parser.add_argument("--out", required=True, help="独立输出目录")
    parser.add_argument("--monthly-usd", type=float, default=500.0,
                        help="固定定投研究金额，默认500美元/月")
    parser.add_argument("--opportunity-monthly-usd", type=float, default=300.0,
                        help="机会资金月度增加额，默认300美元/月")
    parser.add_argument("--opportunity-cap-usd", type=float, default=1800.0,
                        help="机会资金常备上限，默认1800美元")
    parser.add_argument("--opportunity-start-month", default="2026-10",
                        help="机会资金计划开始月份，YYYY-MM，默认2026-10")
    parser.add_argument("--target-day", type=int, default=10)
    parser.add_argument("--overflow-months", type=int, default=6)
    args = parser.parse_args()
    report = run_opportunity_backtest(
        Path(args.prices), Path(args.out),
        monthly_dca_usd=args.monthly_usd,
        opportunity_monthly_usd=args.opportunity_monthly_usd,
        opportunity_cap_usd=args.opportunity_cap_usd,
        opportunity_start_month=args.opportunity_start_month,
        target_day=args.target_day, overflow_dca_months=args.overflow_months,
    )
    print(f"机会资金中文回测报告：{report.resolve()}")


if __name__ == "__main__":
    main()
