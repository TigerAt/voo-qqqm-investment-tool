"""Explainable VOO/QQQM extra-cash guidance from validated daily market data.

This module never creates orders or ledger entries.  It turns the local adjusted
close history, cached market context and the real extra-cash balance into a
bounded tranche plan that still requires the user's configured manual checks.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pandas as pd

import opportunity_rules as opportunity


SYMBOLS = ("VOO", "QQQM")
MACRO_SERIES = {
    "vix": {"ticker": "^VIX", "label": "VIX"},
    "yield_10y_pct": {"ticker": "^TNX", "label": "Cboe 10 年期美债收益率指数"},
}
CENT = Decimal("0.01")


def _decimal(value: object) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("建议计算遇到非有限数字。")
    return result


def _text(value: Decimal) -> str:
    return format(value, "f")


def load_market_context(path: Path) -> dict:
    path = Path(path)
    if not path.is_file():
        return {"values": {}, "warnings": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"values": {}, "warnings": ["市场环境缓存无法读取，请重新更新行情。"]}
    if not isinstance(data, dict) or not isinstance(data.get("values", {}), dict):
        return {"values": {}, "warnings": ["市场环境缓存格式不正确，请重新更新行情。"]}
    data.setdefault("warnings", [])
    return data


def _atomic_json(path: Path, value: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=path.name + ".", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def refresh_market_context(cache_path: Path, as_of: date) -> dict:
    """Refresh VIX and 10Y close values, preserving usable cached values on failure."""
    try:
        import yfinance as yf
    except ImportError as exc:
        raise ValueError("缺少 yfinance，无法更新投资建议所需的市场环境数据。") from exc
    from market_network import direct_yfinance_session

    previous = load_market_context(cache_path)
    previous_values = previous.get("values", {})
    values: dict[str, dict | None] = {}
    warnings: list[str] = []
    # Keep enough history for the manual pre-trade confirmation page to describe
    # 5/20-session changes and a 252-session percentile.  Existing callers only
    # read the latest value, so the additional fields remain backward compatible.
    start = (as_of - timedelta(days=550)).isoformat()
    end = (as_of + timedelta(days=1)).isoformat()
    session = direct_yfinance_session()

    for key, definition in MACRO_SERIES.items():
        try:
            history = yf.Ticker(definition["ticker"], session=session).history(
                start=start, end=end, interval="1d", auto_adjust=False,
                actions=False, keepna=True, timeout=20,
            ).copy()
            if history.empty or "Close" not in history:
                raise ValueError("返回数据为空")
            close = pd.to_numeric(history["Close"], errors="coerce").copy()
            close.index = pd.DatetimeIndex(close.index).tz_localize(None).normalize()
            close = close.loc[close.index <= pd.Timestamp(as_of)]
            close = close.loc[close.map(lambda item: pd.notna(item) and math.isfinite(float(item)) and float(item) > 0)]
            if close.empty:
                raise ValueError(f"截至 {as_of.isoformat()} 没有有效收盘值")
            point_date = close.index[-1].date()
            point_value = float(close.iloc[-1])
            change_1d = (point_value - float(close.iloc[-2])) if len(close) > 1 else None
            change_5d = (point_value - float(close.iloc[-6])) if len(close) > 5 else None
            change_20d = (point_value - float(close.iloc[-21])) if len(close) > 20 else None
            recent = close.tail(252)
            percentile_252 = float((recent <= point_value).mean() * 100)
            ma20 = float(close.tail(20).mean()) if len(close) >= 20 else None
            values[key] = {
                "ticker": definition["ticker"], "label": definition["label"],
                "value": f"{point_value:.4f}", "date": point_date.isoformat(),
                "source": "Yahoo Finance（yfinance）",
                "change_1d": f"{change_1d:.4f}" if change_1d is not None else None,
                "change_5d": f"{change_5d:.4f}" if change_5d is not None else None,
                "change_20d": f"{change_20d:.4f}" if change_20d is not None else None,
                "ma20": f"{ma20:.4f}" if ma20 is not None else None,
                "percentile_252": f"{percentile_252:.2f}",
                "observations": int(len(close)),
            }
            if (as_of - point_date).days > 4:
                warnings.append(f"{definition['label']}仅更新至 {point_date.isoformat()}。")
        except Exception as exc:
            cached = previous_values.get(key)
            if isinstance(cached, dict) and cached.get("value") and cached.get("date"):
                try:
                    cached_date = date.fromisoformat(str(cached["date"]))
                except ValueError:
                    cached_date = None
                if cached_date is not None and cached_date <= as_of:
                    values[key] = cached
                    warnings.append(
                        f"{definition['label']}更新失败，沿用 {cached_date.isoformat()} 的缓存值：{exc}"
                    )
                    continue
            values[key] = None
            warnings.append(f"{definition['label']}更新失败：{exc}")

    result = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "market_as_of": as_of.isoformat(),
        "source": "Yahoo Finance，经 yfinance 获取的日收盘数据",
        "values": values,
        "warnings": warnings,
    }
    _atomic_json(cache_path, result)
    return result


def _read_prices(path: Path) -> pd.DataFrame:
    from market_prices import resolve
    path = resolve(path)
    try:
        prices = pd.read_csv(path, parse_dates=["Date"], index_col="Date")
    except (OSError, ValueError, KeyError) as exc:
        raise ValueError(f"无法读取投资建议行情：{exc}") from exc
    if not set(SYMBOLS).issubset(prices.columns):
        raise ValueError("投资建议行情必须同时包含 VOO 和 QQQM。")
    prices = prices[list(SYMBOLS)].apply(pd.to_numeric, errors="coerce")
    prices.index = pd.DatetimeIndex(prices.index).tz_localize(None).normalize()
    prices = prices.sort_index()
    if prices.index.has_duplicates:
        raise ValueError("投资建议行情包含重复日期。")
    valid = prices.notna().all(axis=1)
    valid &= prices.apply(
        lambda column: column.map(
            lambda item: math.isfinite(float(item)) and float(item) > 0
        )
    ).all(axis=1)
    if not valid.all():
        bad_dates = ", ".join(day.strftime("%Y-%m-%d") for day in prices.index[~valid][:3])
        raise ValueError(f"投资建议行情含缺失或无效价格（{bad_dates}），未生成建议。")
    if len(prices) < 252:
        raise ValueError(f"共同有效行情只有 {len(prices)} 日，至少需要 252 日才能生成建议。")
    from backtest import latest_completed_us_day
    latest_allowed = latest_completed_us_day()
    if prices.index[-1].date() > latest_allowed:
        raise ValueError(
            f"投资建议行情包含尚未完成的日期 {prices.index[-1].date()}，"
            f"当前最多接受到 {latest_allowed}。"
        )
    return prices


def _macro_value(context: dict, key: str, market_date: date,
                 max_business_lag: int = 3) -> tuple[Decimal | None, str | None, int | None]:
    item = context.get("values", {}).get(key)
    if not isinstance(item, dict) or item.get("value") in (None, "") or not item.get("date"):
        return None, None, None
    try:
        item_date = date.fromisoformat(str(item["date"]))
        value = _decimal(item["value"])
    except (ValueError, ArithmeticError):
        return None, None, None
    if item_date > market_date:
        return None, item_date.isoformat(), -1
    if value <= 0:
        return None, item_date.isoformat(), None
    business_lag = len(pd.bdate_range(item_date + timedelta(days=1), market_date))
    if business_lag > max_business_lag:
        return None, item_date.isoformat(), business_lag
    return value, item_date.isoformat(), business_lag


def _rule_checks(symbol_data: dict, vix: Decimal | None, yield_10y: Decimal | None,
                 rules: dict) -> list[dict]:
    checks: list[dict] = []

    def add(key: str, label: str, actual: object, target: object, passed: bool | None) -> None:
        checks.append({"key": key, "label": label, "actual": actual,
                       "target": target, "passed": passed})

    threshold = rules.get("min_52w_drawdown_pct")
    if threshold is not None:
        actual = _decimal(symbol_data["drawdown_52w_pct"])
        target = _decimal(threshold)
        add("min_52w_drawdown_pct", "52 周回撤", _text(actual), f"≥ {_text(target)}%", actual >= target)
    for span in (20, 50, 200):
        setting = rules.get(f"ma{span}_position", "any")
        if setting != "any":
            actual = symbol_data[f"ma{span}_position"]
            add(f"ma{span}_position", f"相对 MA{span}", actual, setting, actual == setting)
    threshold = rules.get("min_vix")
    if threshold is not None:
        target = _decimal(threshold)
        add("min_vix", "VIX", _text(vix) if vix is not None else None,
            f"≥ {_text(target)}", None if vix is None else vix >= target)
    lower, upper = rules.get("min_10y_yield_pct"), rules.get("max_10y_yield_pct")
    if lower is not None:
        target = _decimal(lower)
        add("min_10y_yield_pct", "10Y 收益率下限",
            _text(yield_10y) if yield_10y is not None else None,
            f"≥ {_text(target)}%", None if yield_10y is None else yield_10y >= target)
    if upper is not None:
        target = _decimal(upper)
        add("max_10y_yield_pct", "10Y 收益率上限",
            _text(yield_10y) if yield_10y is not None else None,
            f"≤ {_text(target)}%", None if yield_10y is None else yield_10y <= target)
    return checks


def _row_dict(value: object) -> dict:
    if isinstance(value, dict):
        return dict(value)
    try:
        return dict(value)  # sqlite3.Row
    except (TypeError, ValueError):
        return {}


def _profile_key(value: object) -> str:
    key = str(value or "default")
    return {
        "v1_sensitive": "sensitive",
        "v1_default": "default",
        "v1_conservative": "conservative",
    }.get(key, key)


def _stage_ratio(plan: dict, profile: opportunity.RuleProfile, stage: int) -> Decimal:
    raw = plan.get("stage_ratios_pct")
    if isinstance(raw, (list, tuple)) and len(raw) == profile.stage_count:
        ratios = tuple(_decimal(value) / 100 for value in raw)
        if all(Decimal(0) < value <= Decimal(1) for value in ratios):
            return ratios[stage - 1]
    return profile.deployment_fraction(stage)


def _cycle_state(prices: pd.DataFrame, actions: list[dict], profile: opportunity.RuleProfile,
                 current_rule_hash: str) -> tuple[opportunity.CombinedCycleState, list[str]]:
    """Rebuild stage locks from executed actions and completed historical closes."""
    state = opportunity.CombinedCycleState()
    warnings: list[str] = []
    pending: list[tuple[date, dict]] = []
    for raw in actions:
        item = _row_dict(raw)
        if item.get("status") != "executed" or item.get("rule_hash") != current_rule_hash:
            continue
        # A stage is locked by the real execution, not by the earlier signal.
        # Keeping ``market_date`` only as a fallback preserves older records.
        effective_day = str(item.get("executed_on") or item.get("market_date") or "")
        try:
            pending.append((date.fromisoformat(effective_day), item))
        except ValueError:
            warnings.append(
                f"档位记录 #{item.get('id', '—')} 的执行日期无效，未纳入本轮状态。"
            )
    pending.sort(key=lambda pair: (pair[0], int(pair[1].get("stage") or 0),
                                   int(pair[1].get("id") or 0)))
    closes = {symbol: prices[symbol].tolist() for symbol in profile.symbols}

    def apply_action(item: dict, day: str, index: int) -> None:
        nonlocal state
        drawdowns = {
            symbol: opportunity.rolling_close_signal(closes[symbol], index).drawdown_pct
            for symbol in profile.symbols
        }
        evaluation = opportunity.evaluate_combined_stage(drawdowns, state, profile)
        stage = int(item.get("stage") or 0)
        try:
            # The stored action was server-validated against its signal date.  A
            # later execution can occur after a partial rebound, so replay must
            # retain the audited stage even when that day's drawdown is smaller.
            state = opportunity.record_stage_execution(
                state, stage, current_stage=max(evaluation.combined_stage, stage),
                market_date=day, stage_count=profile.stage_count,
            )
        except (TypeError, ValueError) as exc:
            warnings.append(f"档位记录 #{item.get('id', '—')} 无法纳入本轮状态：{exc}")

    cursor = 0
    for index in range(251, len(prices)):
        market_day = prices.index[index].date()
        day = market_day.isoformat()
        # Weekend/holiday executions become effective before the next completed
        # market close, so that close may count toward a later cycle reset.
        while cursor < len(pending) and pending[cursor][0] < market_day:
            action_day, item = pending[cursor]
            apply_action(item, action_day.isoformat(), index)
            cursor += 1
        strict_highs = []
        if index >= 252:
            for symbol in profile.symbols:
                signal = opportunity.rolling_close_signal(closes[symbol], index)
                if signal.strict_new_high:
                    strict_highs.append(symbol)
        state = opportunity.observe_cycle_day(state, day, strict_highs,
                                              required_symbols=profile.symbols).state
        # A new high from the same close cannot reset a trade executed that day.
        while cursor < len(pending) and pending[cursor][0] == market_day:
            action_day, item = pending[cursor]
            apply_action(item, action_day.isoformat(), index)
            cursor += 1

    # The user may confirm a trade today while the latest completed US close is
    # still yesterday.  Lock it immediately; there are no later closes to use
    # for a reset yet.
    if len(prices) >= 252:
        last_index = len(prices) - 1
        while cursor < len(pending):
            action_day, item = pending[cursor]
            apply_action(item, action_day.isoformat(), last_index)
            cursor += 1
    return state, warnings


def build_advice(price_csv: Path, context_path: Path, *, extra_cash: Decimal,
                 target_weights: dict, position_shares: dict,
                 rules: dict, monthly_budget: Decimal,
                 opportunity_plan: dict | None = None,
                 opportunity_summary: dict | None = None,
                 cny_summary: dict | None = None,
                 stage_actions: list[dict] | None = None,
                 extra_wait_days: int = 0) -> dict:
    """Build USD opportunity-stage guidance without placing or simulating orders.

    ``extra_cash`` is the only investable-cash source.  ``opportunity_summary``
    supplies plan metadata such as the monthly target and optional cash cap; it
    never adds synthetic cash.  ``cny_summary`` remains as a temporary keyword
    compatibility shim for older callers and is intentionally not converted.
    """
    prices = _read_prices(price_csv)
    market_date = prices.index[-1].date()
    context = load_market_context(context_path)
    vix, vix_date, vix_lag = _macro_value(context, "vix", market_date)
    yield_10y, yield_date, yield_lag = _macro_value(context, "yield_10y_pct", market_date)
    plan = dict(opportunity_plan or {})
    profile = opportunity.get_profile(_profile_key(plan.get("rule_profile")))
    material_plan = {
        "stage_scope": plan.get("stage_scope", "combined"),
        "stage_ratios_pct": plan.get("stage_ratios_pct", [20, 25, 25, 30]),
        "reset_rule": plan.get("reset_rule", "both_symbols_new_252d_high"),
        "half_multiplier_pct": plan.get("risk_half_multiplier_pct", 50),
    }
    current_rule_hash = opportunity.rule_hash(profile, material_plan)
    action_rows = [_row_dict(item) for item in (stage_actions or [])]
    cycle_state, replay_warnings = _cycle_state(prices, action_rows, profile, current_rule_hash)
    cycle_key = f"{current_rule_hash[:12]}-{cycle_state.cycle_number}"

    extra_cash = max(Decimal(0), _decimal(extra_cash))
    plan_summary = dict(opportunity_summary or {})
    # Never reinterpret legacy CNY values as USD.  Keeping the old keyword in
    # the signature lets callers migrate independently without corrupting units.
    _ = cny_summary
    monthly_target_raw = plan_summary.get(
        "monthly_target_usd",
        plan.get("monthly_usd", plan.get("monthly_target_usd", 0)),
    )
    monthly_target_usd = max(Decimal(0), _decimal(monthly_target_raw or 0))
    opportunity_start = (
        plan_summary.get("start_month") or plan_summary.get("plan_start")
        or plan.get("start_month") or plan.get("plan_start")
    )
    plan_active = bool(plan_summary.get("active", True))
    cap_raw = plan_summary.get("cap_usd", plan.get("cap_usd"))
    cap_usd = None if cap_raw in (None, "") else max(Decimal(0), _decimal(cap_raw))
    deployable_usd = (
        extra_cash if cap_usd is None else min(extra_cash, cap_usd)
    ) if plan_active else Decimal(0)
    market_values = {
        symbol: _decimal(prices[symbol].iloc[-1]) * max(Decimal(0), _decimal(position_shares.get(symbol, 0)))
        for symbol in SYMBOLS
    }
    total_market_value = sum(market_values.values(), Decimal(0))
    symbol_payloads: list[dict] = []
    drawdowns: dict[str, Decimal] = {}
    supplemental_pass = True

    for symbol in SYMBOLS:
        series = prices[symbol]
        signal = opportunity.rolling_close_signal(series.tolist())
        drawdowns[symbol] = signal.drawdown_pct
        tail = series.tail(252)
        high_timestamp = tail.idxmax()
        current_allocation = market_values[symbol] / total_market_value * 100 if total_market_value else Decimal(0)
        data = {
            "symbol": symbol, "close": _text(signal.close),
            "high_52w": _text(signal.rolling_high), "high_52w_date": high_timestamp.date().isoformat(),
            "drawdown_52w_pct": _text(signal.drawdown_pct),
            "current_stage": opportunity.stage_for_drawdown(symbol, signal.drawdown_pct, profile),
            "thresholds_pct": [_text(value) for value in profile.thresholds_for(symbol)],
            "return_1m_pct": _text((_decimal(series.iloc[-1]) / _decimal(series.iloc[-22]) - 1) * 100),
            "return_3m_pct": _text((_decimal(series.iloc[-1]) / _decimal(series.iloc[-64]) - 1) * 100),
            "current_allocation_pct": _text(current_allocation),
            "target_pct": _text(_decimal(target_weights[symbol])),
            "drift_pct_points": _text(current_allocation - _decimal(target_weights[symbol])),
        }
        below_labels = []
        for span in (20, 50, 200):
            average = _decimal(series.tail(span).mean())
            position = "above" if signal.close >= average else "below"
            data[f"ma{span}"] = _text(average)
            data[f"ma{span}_position"] = position
            data[f"ma{span}_distance_pct"] = _text((signal.close / average - 1) * 100)
            if position == "below":
                below_labels.append(f"MA{span}")
        checks = _rule_checks(data, vix, yield_10y, rules)
        rules_passed = (None if not checks else False if any(item["passed"] is None for item in checks)
                        else all(bool(item["passed"]) for item in checks))
        if checks and rules_passed is not True:
            supplemental_pass = False
        reference_prices = {}
        for index, threshold in enumerate(profile.thresholds_for(symbol), start=1):
            reference_prices[f"stage_{index}"] = _text(
                (signal.rolling_high * (Decimal(1) - threshold / 100)).quantize(Decimal("0.0001"))
            )
        reasons = [f"距近 252 个交易日最高收盘价回撤 {signal.drawdown_pct:.1f}%"]
        reasons.append("低于 " + "、".join(below_labels) if below_labels else "仍高于 20/50/200 日均线")
        data.update({
            "rules_configured": bool(checks), "rules_passed": rules_passed,
            "rule_checks": checks, "reference_prices": reference_prices,
            "reasons": reasons, "suggested_amount": "0.00",
        })
        symbol_payloads.append(data)

    evaluation = opportunity.evaluate_combined_stage(drawdowns, cycle_state, profile)
    candidate_stage = evaluation.next_stage
    ratio = _stage_ratio(plan, profile, candidate_stage) if candidate_stage else Decimal(0)
    rule_amount_usd = (deployable_usd * ratio).quantize(CENT, rounding=ROUND_HALF_UP)
    candidate_action = None
    if candidate_stage:
        matching = [row for row in action_rows if row.get("cycle_key") == cycle_key
                    and int(row.get("stage") or 0) == candidate_stage
                    and row.get("status") not in ("cancelled", "executed")]
        if matching:
            candidate_action = sorted(matching, key=lambda row: int(row.get("id") or 0))[-1]
            # A saved review freezes the USD rule basis for audit.  Later buys
            # reduce current cash but must not silently rewrite the approved
            # tranche or its 80/20 target split.
            if candidate_action.get("pool_usd_before") not in (None, ""):
                deployable_usd = max(Decimal(0), _decimal(candidate_action["pool_usd_before"]))
            if candidate_action.get("rule_amount_usd") not in (None, ""):
                rule_amount_usd = max(Decimal(0), _decimal(candidate_action["rule_amount_usd"]))

    half_multiplier = _decimal(plan.get("risk_half_multiplier_pct", 50)) / 100
    decision = opportunity.conservative_risk_decision(None, half_multiplier=half_multiplier)
    approved_usd = Decimal(0)
    recorded_approved_usd = Decimal(0)
    if candidate_action:
        review = candidate_action.get("review", candidate_action.get("review_json"))
        if isinstance(review, str):
            try:
                review = json.loads(review)
            except json.JSONDecodeError:
                review = None
        decision = opportunity.conservative_risk_decision(
            review if isinstance(review, dict) else None,
            half_multiplier=half_multiplier,
            require_note=bool(plan.get("require_risk_review", True)),
        )
        raw_approved = candidate_action.get("approved_amount_usd")
        if raw_approved not in (None, ""):
            recorded_approved_usd = max(Decimal(0), _decimal(raw_approved))

    effective_usd = (rule_amount_usd * decision.multiplier).quantize(
        CENT, rounding=ROUND_HALF_UP
    )
    if candidate_action and candidate_action.get("effective_amount_usd") not in (None, ""):
        effective_usd = max(
            Decimal(0), _decimal(candidate_action["effective_amount_usd"])
        )
    approval_limit = min(deployable_usd, effective_usd)
    approved_usd = min(recorded_approved_usd, approval_limit)
    approval_was_capped = recorded_approved_usd > approval_limit

    target_allocations = opportunity.allocate_80_20(approved_usd)
    linked_allocations = {symbol: Decimal(0) for symbol in SYMBOLS}
    if candidate_action and isinstance(candidate_action.get("linked_amounts_usd"), dict):
        for symbol in SYMBOLS:
            linked_allocations[symbol] = max(
                Decimal(0), _decimal(candidate_action["linked_amounts_usd"].get(symbol, 0))
            )
    remaining_targets = {
        symbol: max(Decimal(0), target_allocations[symbol] - linked_allocations[symbol])
        for symbol in SYMBOLS
    }
    remaining_target_total = sum(remaining_targets.values(), Decimal(0))
    age_days = max(0, (date.today() - market_date).days)
    executable = min(extra_cash, remaining_target_total)
    if (age_days > 7 or not decision.complete or decision.action == "pause" or
            not supplemental_pass or not candidate_stage or
            not candidate_action or candidate_action.get("status") != "approved"):
        executable = Decimal(0)
    allocations = {symbol: Decimal(0) for symbol in SYMBOLS}
    if executable > 0 and remaining_target_total > 0:
        remaining = executable
        for index, symbol in enumerate(SYMBOLS):
            if index == len(SYMBOLS) - 1:
                part = min(remaining_targets[symbol], remaining)
            else:
                part = (executable * remaining_targets[symbol] / remaining_target_total).quantize(
                    CENT, rounding=ROUND_HALF_UP
                )
                part = min(remaining_targets[symbol], remaining, max(Decimal(0), part))
            allocations[symbol] = part
            remaining -= part
        if remaining > 0:
            for symbol in reversed(SYMBOLS):
                room = remaining_targets[symbol] - allocations[symbol]
                add = min(room, remaining)
                allocations[symbol] += add
                remaining -= add
                if remaining <= 0:
                    break
    by_symbol = {item["symbol"]: item for item in symbol_payloads}
    for symbol, amount in allocations.items():
        by_symbol[symbol]["suggested_amount"] = _text(amount)

    executed = set(cycle_state.executed_stages)
    stage_statuses = []
    for stage in range(1, profile.stage_count + 1):
        stage_statuses.append({
            "stage": stage,
            "ratio_pct": _text(_stage_ratio(plan, profile, stage) * 100),
            "reached": evaluation.combined_stage >= stage,
            "executed": stage in executed,
            "current_candidate": stage == candidate_stage,
        })

    if evaluation.combined_stage == 0:
        headline = "当前未进入机会资金档位"
        opportunity_clause = (f"机会资金计划从 {opportunity_start} 起每月 ${monthly_target_usd:,.0f}；"
                              if not plan_active and opportunity_start else
            f"机会资金按每月 ${monthly_target_usd:,.0f} 的计划继续积累；"
            if monthly_target_usd > 0 else "机会资金继续积累；"
        )
        summary = (
            "VOO 与 QQQM 都在第一档回撤线之上；"
            f"{opportunity_clause}每月 ${monthly_budget:,.0f} 固定定投照常执行。"
        )
    elif candidate_stage is None:
        headline = "当前已触发档位均已执行"
        summary = "本轮不会重复使用相同档位；等待更深回撤，或等待两只 ETF 均出现新的 52 周收盘新高后开启新周期。"
    elif not plan_active:
        headline = f"已触发第 {candidate_stage} 档，但机会资金计划尚未开始"
        summary = f"计划从 {opportunity_start or '设定月份'} 起执行；开始前不会生成可执行金额。"
    elif rule_amount_usd <= 0:
        headline = f"已触发第 {candidate_stage} 档，但额外美元资金池为空"
        summary = "先记录 Schwab 实际到账的额外美元；月度计划不会自动增加可交易现金。"
    elif candidate_action is None:
        headline = f"已触发第 {candidate_stage} 档，等待风险复核"
        summary = "先检查盈利预期、流动性与利率环境；完成复核后，才会给出可执行美元上限。"
    elif decision.action == "pause" or not decision.complete:
        headline = f"第 {candidate_stage} 档暂缓执行"
        summary = "风险复核尚未完成或结果为暂停。本档没有锁定，后续可以重新评估。"
    elif extra_cash <= 0:
        headline = f"第 {candidate_stage} 档已复核，等待额外美元到账"
        summary = "请先记录 Schwab 实际到账的额外美元，再记录真实买入。"
    elif (candidate_action and candidate_action.get("status") == "approved" and
          candidate_action.get("transaction_ids") and remaining_target_total <= Decimal("0.02")):
        headline = f"第 {candidate_stage} 档真实买入已记录"
        summary = "请核对 VOO/QQQM 实际金额后确认本档执行完成；确认前档位仍未锁定。"
    elif executable <= 0:
        headline = f"第 {candidate_stage} 档暂不可执行"
        summary = "请核对补充条件、复核批准金额和真实额外美元余额。"
    else:
        headline = f"第 {candidate_stage} 档可分批执行，美元上限 ${executable:,.2f}"
        summary = "按 VOO 80% / QQQM 20% 记录真实成交；两笔成交完成并确认后，本档才会锁定。"

    warnings = [str(item) for item in context.get("warnings", []) if item]
    warnings.extend(replay_warnings)
    context_as_of = context.get("market_as_of")
    if context_as_of and context_as_of != market_date.isoformat():
        warnings.append(f"市场环境缓存对应 {context_as_of}，与 ETF 行情日 {market_date.isoformat()} 不一致。")
    if age_days > 7:
        warnings.append(f"ETF 行情截至 {market_date.isoformat()}，距今天已 {age_days} 天；已禁止给出可执行金额。")
    if vix is None:
        suffix = f"（最近日期 {vix_date}）" if vix_date else ""
        warnings.append(f"VIX 暂无与 ETF 行情同步的可用值{suffix}；仅缺少辅助观察，不改变价格档位。")
    if yield_10y is None:
        suffix = f"（最近日期 {yield_date}）" if yield_date else ""
        warnings.append(f"10 年期收益率暂无同步值{suffix}；请在风险复核中人工判断利率环境。")
    if extra_cash > 0 and extra_wait_days >= 90:
        warnings.append(f"额外美元已等待 {extra_wait_days} 天，请复核继续等待的机会成本。")
    if not plan_active:
        warnings.append(f"机会资金计划从 {opportunity_start or '设定月份'} 起生效；当前不开放档位金额。")
    if not supplemental_pass:
        warnings.append("已启用的补充量化条件尚未全部满足；本次可执行金额为 0。")
    if cap_usd is not None and extra_cash > cap_usd:
        warnings.append(
            f"额外美元现金 ${extra_cash:,.2f} 超过计划上限 ${cap_usd:,.2f}；"
            "档位金额只按上限内资金计算。"
        )
    if approval_was_capped:
        warnings.append(
            f"已记录的批准金额 ${recorded_approved_usd:,.2f} 超过风险调整后上限 "
            f"${approval_limit:,.2f}；本次建议已按较低上限计算，请重新保存风险复核。"
        )

    candidate = None if candidate_stage is None else {
        "stage": candidate_stage, "cycle_key": cycle_key,
        "trigger_symbols": list(evaluation.trigger_symbols),
        "ratio_pct": _text(ratio * 100), "rule_amount_usd": _text(rule_amount_usd),
        "pool_usd_before": _text(deployable_usd),
        "effective_amount_usd": _text(effective_usd),
        "action_id": candidate_action.get("id") if candidate_action else None,
        "action_status": candidate_action.get("status") if candidate_action else "pending_review",
        "risk_action": decision.action, "risk_complete": decision.complete,
        "risk_reasons": list(decision.reasons),
        "approved_amount_usd": _text(approved_usd),
        "recorded_approved_amount_usd": _text(recorded_approved_usd),
        "suggested_total_usd": _text(executable),
        "allocations_usd": {symbol: _text(amount) for symbol, amount in allocations.items()},
        "target_allocations_usd": {
            symbol: _text(amount) for symbol, amount in target_allocations.items()
        },
        "linked_allocations_usd": {
            symbol: _text(amount) for symbol, amount in linked_allocations.items()
        },
        "remaining_allocations_usd": {
            symbol: _text(amount) for symbol, amount in remaining_targets.items()
        },
    }
    return {
        "status": "stale" if age_days > 7 else "ready",
        "market_as_of": market_date.isoformat(), "age_days": age_days,
        "generated_at": context.get("generated_at"),
        "source": "VOO/QQQM：Yahoo Finance 自动复权日收盘价；VIX/10Y：Yahoo Finance 日收盘值（仅作辅助）",
        "headline": headline, "summary": summary,
        "extra_cash": _text(extra_cash), "suggested_total": _text(executable),
        "opportunity_cash_usd": _text(extra_cash),
        "opportunity_cap_usd": _text(cap_usd) if cap_usd is not None else None,
        "deployable_usd": _text(min(extra_cash, cap_usd) if cap_usd is not None else extra_cash),
        "opportunity_monthly_target_usd": _text(monthly_target_usd),
        "opportunity_plan_start": str(opportunity_start) if opportunity_start else None,
        "opportunity_plan_active": plan_active,
        "combined_stage": evaluation.combined_stage,
        "next_stage": candidate_stage, "deploy_pct": _text(ratio * 100),
        "cycle_key": cycle_key, "cycle_number": cycle_state.cycle_number,
        "executed_stages": list(cycle_state.executed_stages),
        "new_high_symbols": list(cycle_state.new_high_symbols),
        "stage_statuses": stage_statuses, "candidate": candidate,
        "rule_profile": profile.as_payload(), "rule_hash": current_rule_hash,
        "macro": {
            "vix": _text(vix) if vix is not None else None, "vix_date": vix_date,
            "yield_10y_pct": _text(yield_10y) if yield_10y is not None else None,
            "yield_10y_date": yield_date,
        },
        "manual_requirements": {"risk_review": True, "manual_confirmation": True},
        "extra_wait_days": extra_wait_days,
        "symbols": symbol_payloads, "warnings": warnings,
    }


def unavailable_advice(message: str) -> dict:
    return {"status": "unavailable", "headline": "暂时无法生成投资建议",
            "summary": message, "symbols": [], "warnings": [message]}
