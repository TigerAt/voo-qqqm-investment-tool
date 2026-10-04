"""Manual final market check for fixed DCA and opportunity funds.

The module only reads validated daily market inputs and writes a confirmation cache.
It never creates ledger entries, approves opportunity stages, or places orders.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from datetime import date, datetime
from pathlib import Path

import pandas as pd

import backtest
import market_advice


SCHEMA_VERSION = 4
SYMBOLS = ("VOO", "QQQM")
PURPOSES = ("fixed_dca", "opportunity")


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _text(value: float | int | None, digits: int = 6) -> str | None:
    if value is None or not math.isfinite(float(value)):
        return None
    rendered = f"{float(value):.{digits}f}".rstrip("0").rstrip(".")
    return rendered if rendered not in ("", "-0") else "0"


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


def save_analysis(path: Path, value: dict) -> None:
    """Atomically save one purpose-specific pre-trade confirmation."""
    purpose = value.get("purpose") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION
            or purpose not in PURPOSES):
        raise ValueError("下单前核对结果格式不正确，未保存。")
    path = Path(path)
    container = {"schema_version": SCHEMA_VERSION, "results": {}}
    if path.is_file():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            previous = None
        if (isinstance(previous, dict)
                and previous.get("schema_version") == SCHEMA_VERSION
                and isinstance(previous.get("results"), dict)):
            container = previous
    container["results"][purpose] = value
    container["latest_purpose"] = purpose
    container["updated_at"] = value.get("generated_at")
    _atomic_json(path, container)


def file_sha256(path: Path) -> str | None:
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def advice_signature(advice: dict) -> str | None:
    """Hash every advice field that can change this analysis' conclusion."""
    if not isinstance(advice, dict):
        return None
    candidate = advice.get("candidate")
    if not isinstance(candidate, dict):
        candidate = None
    symbols = []
    for item in advice.get("symbols") or []:
        if not isinstance(item, dict) or item.get("symbol") not in SYMBOLS:
            continue
        symbols.append({
            "symbol": item.get("symbol"),
            "current_stage": item.get("current_stage"),
            "rules_configured": item.get("rules_configured"),
            "rules_passed": item.get("rules_passed"),
            "rule_checks": item.get("rule_checks"),
        })
    payload = {
        "status": advice.get("status"),
        "market_as_of": advice.get("market_as_of"),
        "rule_hash": advice.get("rule_hash"),
        "combined_stage": advice.get("combined_stage"),
        "next_stage": advice.get("next_stage"),
        "cycle_key": advice.get("cycle_key"),
        "cycle_number": advice.get("cycle_number"),
        "executed_stages": advice.get("executed_stages"),
        "extra_cash": advice.get("extra_cash"),
        "opportunity_cash_usd": advice.get("opportunity_cash_usd"),
        "opportunity_cap_usd": advice.get("opportunity_cap_usd"),
        "deployable_usd": advice.get("deployable_usd"),
        "opportunity_monthly_target_usd": advice.get(
            "opportunity_monthly_target_usd"
        ),
        "opportunity_plan_start": advice.get("opportunity_plan_start"),
        "opportunity_plan_active": advice.get("opportunity_plan_active"),
        "symbols": symbols,
        "candidate": candidate,
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def plan_signature(plan: dict) -> str | None:
    if not isinstance(plan, dict):
        return None
    canonical = json.dumps(
        plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _market_lag_business_days(market_as_of: str) -> int | None:
    try:
        market_day = date.fromisoformat(str(market_as_of))
    except ValueError:
        return None
    latest = backtest.latest_completed_us_day()
    if market_day > latest:
        return -1
    return len(pd.bdate_range(market_day + pd.Timedelta(days=1), latest))


def _empty_result(purpose: str, status: str = "never_run", message: str | None = None) -> dict:
    labels = {"fixed_dca": "本月固定定投", "opportunity": "机会资金交易"}
    return {
        "schema_version": SCHEMA_VERSION,
        "purpose": purpose,
        "purpose_label": labels[purpose],
        "status": status,
        "headline": "尚未执行下单前核对" if status == "never_run" else "上次下单前核对无法读取",
        "summary": message or "点击“联网更新并核对”后生成本次市场快照。",
        "stale": status != "never_run",
    }


def _evaluate_cached_result(
    raw: object, purpose: str, *, current_market_as_of: str | None,
    current_rule_hash: str | None, current_price_sha256: str | None,
    current_context_sha256: str | None, current_advice_signature: str | None,
    current_advice_status: str | None, current_plan_signature: str | None,
) -> dict:
    if not isinstance(raw, dict) or raw.get("purpose") != purpose:
        return _empty_result(purpose)
    result = dict(raw)
    market_as_of = str(result.get("market_as_of") or "")
    stale_reasons: list[str] = []
    lag = _market_lag_business_days(market_as_of)
    try:
        parsed_market_day = date.fromisoformat(market_as_of)
    except ValueError:
        parsed_market_day = None
    result["age_days"] = (
        max(0, (date.today() - parsed_market_day).days) if parsed_market_day else None
    )
    result["market_lag_business_days"] = lag
    try:
        generated_day = datetime.fromisoformat(str(result.get("generated_at") or "")).date()
    except ValueError:
        generated_day = None
    if generated_day != date.today():
        stale_reasons.append("下单前核对不是今天执行的")
    if (current_price_sha256 != result.get("price_file_sha256")):
        stale_reasons.append("本地 ETF 行情内容已变化")
    if current_context_sha256 != result.get("context_file_sha256"):
        stale_reasons.append("VIX 或 10Y 市场环境数据已变化")
    if purpose == "fixed_dca":
        if current_plan_signature != result.get("plan_signature"):
            stale_reasons.append("本月固定定投进度或计划已变化")
    else:
        if current_advice_status not in (None, "ready"):
            stale_reasons.append("当前投资建议无法作为有效确认基准")
        if current_market_as_of and market_as_of and current_market_as_of != market_as_of:
            stale_reasons.append("投资建议已使用不同日期的行情")
        if current_rule_hash != result.get("rule_hash"):
            stale_reasons.append("机会资金档位规则已变化")
        if current_advice_signature != result.get("advice_signature"):
            stale_reasons.append("候选档位、批准金额、资金或执行状态已变化")
    if lag is None or lag < 0:
        stale_reasons.append("行情日期无法验证")
    elif lag > 1:
        stale_reasons.append(f"行情落后最近完整美股交易日 {lag} 个工作日")
    result["stale"] = bool(stale_reasons)
    if stale_reasons:
        result["warnings"] = list(dict.fromkeys([
            "；".join(stale_reasons) + "；请重新执行下单前核对。",
            *(result.get("warnings") or []),
        ]))
    return result


def load_analysis(path: Path, *, current_market_as_of: str | None = None,
                  current_rule_hash: str | None = None,
                  current_price_sha256: str | None = None,
                  current_context_sha256: str | None = None,
                  current_advice_signature: str | None = None,
                  current_advice_status: str | None = None,
                  current_plan_signature: str | None = None) -> dict:
    """Load both cached confirmations without calculating or using the network."""
    path = Path(path)
    container: dict = {"schema_version": SCHEMA_VERSION, "results": {}}
    if path.is_file():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            value = None
        if (isinstance(value, dict) and value.get("schema_version") == SCHEMA_VERSION
                and isinstance(value.get("results"), dict)):
            container = value
        elif value is not None:
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "bundle",
                "latest_purpose": "fixed_dca",
                "results": {
                    purpose: _empty_result(
                        purpose,
                        message="旧版市场分析结果不再沿用，请重新执行下单前核对。",
                    ) for purpose in PURPOSES
                },
            }
    results = {
        purpose: _evaluate_cached_result(
            container.get("results", {}).get(purpose), purpose,
            current_market_as_of=current_market_as_of,
            current_rule_hash=current_rule_hash,
            current_price_sha256=current_price_sha256,
            current_context_sha256=current_context_sha256,
            current_advice_signature=current_advice_signature,
            current_advice_status=current_advice_status,
            current_plan_signature=current_plan_signature,
        ) for purpose in PURPOSES
    }
    latest = container.get("latest_purpose")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "bundle",
        "latest_purpose": latest if latest in PURPOSES else "fixed_dca",
        "results": results,
    }


def _return_pct(series: pd.Series, sessions: int) -> float | None:
    if len(series) <= sessions:
        return None
    start = _finite(series.iloc[-1 - sessions])
    end = _finite(series.iloc[-1])
    if start is None or end is None or start <= 0:
        return None
    return (end / start - 1) * 100


def _rsi14(series: pd.Series) -> float | None:
    changes = series.diff().dropna()
    if len(changes) < 14:
        return None
    gains = changes.clip(lower=0)
    losses = -changes.clip(upper=0)
    average_gain = float(gains.iloc[:14].mean())
    average_loss = float(losses.iloc[:14].mean())
    for index in range(14, len(changes)):
        average_gain = (average_gain * 13 + float(gains.iloc[index])) / 14
        average_loss = (average_loss * 13 + float(losses.iloc[index])) / 14
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    strength = average_gain / average_loss
    return float(100 - 100 / (1 + strength))


def _trend(close: float, ma20: float, ma50: float, ma200: float) -> tuple[str, str]:
    if close > ma20 >= ma50 >= ma200:
        return "uptrend", "多周期上行结构"
    if close < ma20 < ma50 < ma200:
        return "broad_downtrend", "广泛下行结构"
    if close < ma20 and close < ma50 and close >= ma200:
        return "long_term_pullback", "长期趋势内回调"
    if close < ma200:
        return "long_term_pressure", "长期趋势承压"
    return "mixed", "趋势结构混合"


def _macro_payload(context: dict, key: str) -> dict:
    raw = (context.get("values") or {}).get(key)
    if not isinstance(raw, dict):
        return {"value": None, "date": None, "change_1d": None, "change_5d": None,
                "change_20d": None, "percentile_252": None}
    return {
        "value": _text(_finite(raw.get("value")), 4),
        "date": raw.get("date"),
        "change_1d": _text(_finite(raw.get("change_1d")), 4),
        "change_5d": _text(_finite(raw.get("change_5d")), 4),
        "change_20d": _text(_finite(raw.get("change_20d")), 4),
        "ma20": _text(_finite(raw.get("ma20")), 4),
        "percentile_252": _text(_finite(raw.get("percentile_252")), 2),
        "observations": raw.get("observations"),
        "source": raw.get("source"),
    }


def _vix_label(value: float | None) -> str:
    if value is None:
        return "资料不足"
    if value < 15:
        return "平静"
    if value < 20:
        return "常态"
    if value < 30:
        return "风险偏好下降"
    if value < 40:
        return "高压力"
    return "极端压力"


def _yield_label(value: float | None) -> str:
    if value is None:
        return "资料不足"
    if value < 4:
        return "较低"
    if value < 4.5:
        return "中等"
    if value < 5:
        return "偏高"
    return "高位"


def _yield_trend(change_20d: float | None) -> str:
    if change_20d is None:
        return "趋势资料不足"
    basis_points = change_20d * 100
    if basis_points >= 30:
        return "快速上行"
    if basis_points >= 15:
        return "上行"
    if basis_points <= -15:
        return "回落"
    return "大致稳定"


def build_pretrade_confirmation(
    price_csv: Path, context_path: Path, advice: dict, *, purpose: str,
    plan: dict | None = None, preview: bool = False,
) -> dict:
    """Build a concise final market check for one intended manual trade."""
    if purpose not in PURPOSES:
        raise ValueError("下单前核对用途须为固定定投或机会资金。")
    prices = market_advice._read_prices(price_csv)
    market_as_of = prices.index[-1].date().isoformat()
    market_day = date.fromisoformat(market_as_of)
    market_lag = _market_lag_business_days(market_as_of)
    market_fresh = market_lag is not None and 0 <= market_lag <= 1
    plan_data = dict(plan or {})

    symbols: list[dict] = []
    attention: list[dict] = []
    for symbol in SYMBOLS:
        series = prices[symbol].astype(float)
        close = float(series.iloc[-1])
        ma20 = float(series.tail(20).mean())
        ma50 = float(series.tail(50).mean())
        ma200 = float(series.tail(200).mean())
        trend_code, trend_label = _trend(close, ma20, ma50, ma200)
        returns = series.pct_change().dropna()
        day_change = _return_pct(series, 1)
        five_day_change = _return_pct(series, 5)
        one_month_change = _return_pct(series, 21)
        volatility = float(returns.tail(20).std(ddof=1) * math.sqrt(252) * 100)
        if day_change is not None and abs(day_change) >= 3:
            attention.append({
                "status": "warn",
                "detail": f"{symbol} 最新完整交易日涨跌 {day_change:+.1f}%，下单前重点核对实时价格和价差。",
            })
        symbols.append({
            "symbol": symbol,
            "close": _text(close, 4),
            "date": market_as_of,
            "change_1d_pct": _text(day_change, 4),
            "change_5d_pct": _text(five_day_change, 4),
            "change_1m_pct": _text(one_month_change, 4),
            "trend_code": trend_code,
            "trend_label": trend_label,
            "ma20": _text(ma20, 4),
            "ma50": _text(ma50, 4),
            "ma200": _text(ma200, 4),
            "ma20_distance_pct": _text((close / ma20 - 1) * 100, 4),
            "ma50_distance_pct": _text((close / ma50 - 1) * 100, 4),
            "ma200_distance_pct": _text((close / ma200 - 1) * 100, 4),
            "volatility_20d_pct": _text(volatility, 4),
            "rsi14": _text(_rsi14(series), 2),
        })

    context = market_advice.load_market_context(context_path)
    vix = _macro_payload(context, "vix")
    yield10 = _macro_payload(context, "yield_10y_pct")
    vix_effective, _, vix_lag = market_advice._macro_value(context, "vix", market_day)
    yield_effective, _, yield_lag = market_advice._macro_value(
        context, "yield_10y_pct", market_day
    )
    vix_value = float(vix_effective) if vix_effective is not None else None
    yield_value = float(yield_effective) if yield_effective is not None else None
    yield_change_20d = _finite(yield10.get("change_20d")) if yield_value is not None else None
    vix.update({
        "valid": vix_value is not None,
        "lag_business_days": vix_lag,
        "label": _vix_label(vix_value),
    })
    yield10.update({
        "valid": yield_value is not None,
        "lag_business_days": yield_lag,
        "label": _yield_label(yield_value),
        "trend_label": _yield_trend(yield_change_20d),
        "change_1d_bp": _text(_finite(yield10.get("change_1d")) * 100, 2)
        if _finite(yield10.get("change_1d")) is not None else None,
        "change_5d_bp": _text(_finite(yield10.get("change_5d")) * 100, 2)
        if _finite(yield10.get("change_5d")) is not None else None,
        "change_20d_bp": _text(yield_change_20d * 100, 2)
        if yield_change_20d is not None else None,
    })
    macro_complete = (
        vix_value is not None and yield_value is not None
        and _finite(vix.get("change_5d")) is not None
        and _finite(yield10.get("change_5d")) is not None
        and not context.get("warnings")
    )
    if vix_value is not None and vix_value >= 30:
        attention.append({
            "status": "warn" if vix_value < 40 else "fail",
            "detail": f"VIX 为 {vix_value:.2f}，市场波动压力较高。",
        })
    if (yield_value is not None and yield_value >= 4.5
            and yield_change_20d is not None and yield_change_20d >= 0.30):
        attention.append({
            "status": "warn",
            "detail": (
                f"美国 10Y 为 {yield_value:.3f}%，近 20 日上升 "
                f"{yield_change_20d * 100:.0f}bp，成长股估值压力需要复核。"
            ),
        })
    if not market_fresh:
        attention.append({
            "status": "fail",
            "detail": "ETF 行情不是最近 0–1 个工作日内的完整美股日线。",
        })
    if not macro_complete:
        attention.append({
            "status": "warn",
            "detail": "VIX、10Y 或短期变化资料不完整；价格快照仍可查看，环境确认不完整。",
        })

    checks: list[dict] = [{
        "name": "行情时效",
        "status": "pass" if market_fresh else "fail",
        "detail": (
            f"截至 {market_as_of}，落后最近完整美股交易日 {market_lag} 个工作日"
            if market_lag is not None and market_lag >= 0 else "行情日期无法验证"
        ),
    }, {
        "name": "市场环境资料",
        "status": "pass" if macro_complete else "warn",
        "detail": f"VIX {vix.get('date') or '缺失'}；10Y {yield10.get('date') or '缺失'}",
    }]

    target: dict
    if purpose == "fixed_dca":
        budget = _finite(plan_data.get("budget")) or 0.0
        bought = _finite(plan_data.get("bought")) or 0.0
        remaining = max(0.0, _finite(plan_data.get("remaining")) or 0.0)
        fixed_cash = max(0.0, _finite(plan_data.get("fixed_cash")) or 0.0)
        month = str(plan_data.get("month") or "")
        completed = budget > 0 and bought + 0.005 >= budget
        target = {
            "plan_month": month,
            "target_day": plan_data.get("target_day"),
            "budget_usd": _text(budget, 2),
            "bought_usd": _text(bought, 2),
            "remaining_usd": _text(remaining, 2),
            "fixed_cash_usd": _text(fixed_cash, 2),
            "arrival_date": plan_data.get("arrival"),
            "last_trade_date": plan_data.get("trade"),
            "target_weights": plan_data.get("target_weights") or {},
        }
        checks.extend([{
            "name": "本月固定定投",
            "status": "pass" if completed else "warn" if fixed_cash < remaining else "pass",
            "detail": f"已买入 ${bought:,.2f} / ${budget:,.2f}，剩余 ${remaining:,.2f}",
        }, {
            "name": "固定定投资金",
            "status": "pass" if completed or fixed_cash + 0.005 >= remaining else "warn",
            "detail": f"已记录待投资现金 ${fixed_cash:,.2f}",
        }])
        if completed:
            confirmation = "fixed_completed"
            headline = "本月固定定投已经完成"
            summary = "本页继续提供市场背景；无需重复执行本月固定定投。"
        elif not market_fresh:
            confirmation = "fixed_market_data_attention"
            headline = "行情资料需要更新"
            summary = "固定定投规则不变；请取得最新完整日线，并在 Schwab 核对实时报价后执行。"
        elif fixed_cash + 0.005 < remaining:
            confirmation = "fixed_awaiting_funds"
            headline = "等待固定定投资金到账"
            summary = "市场背景已更新；资金到账后仍按固定定投计划执行，不需要等待价格信号。"
        elif attention:
            confirmation = "fixed_market_attention"
            headline = "固定定投计划不变，当前市场需要仔细核价"
            summary = "行情不会改变每月定投规则；下单时请重点核对 Schwab 实时报价、价差与订单金额。"
        else:
            confirmation = "fixed_ready_for_manual_entry"
            headline = "本月固定定投可按计划执行"
            summary = "市场快照已更新；固定定投不择时，正式下单前请在 Schwab 核对实时价格与价差。"
        subject_key = f"fixed:{month}"
    else:
        candidate = advice.get("candidate") if isinstance(advice, dict) else None
        candidate = candidate if isinstance(candidate, dict) else None
        advice_ready = advice.get("status") == "ready" and advice.get("market_as_of") == market_as_of
        supplemental_ok = all(
            not item.get("rules_configured") or item.get("rules_passed") is True
            for item in advice.get("symbols") or [] if isinstance(item, dict)
        )
        action_status = candidate.get("action_status") if candidate else None
        risk_complete = candidate.get("risk_complete") is True if candidate else False
        approved = _finite(candidate.get("approved_amount_usd")) if candidate else None
        executable = _finite(candidate.get("suggested_total_usd")) if candidate else None
        target = {
            "cycle_key": advice.get("cycle_key"),
            "stage": candidate.get("stage") if candidate else None,
            "action_id": candidate.get("action_id") if candidate else None,
            "action_status": action_status,
            "risk_complete": risk_complete,
            "approved_amount_usd": _text(approved, 2),
            "executable_amount_usd": _text(executable, 2),
            "allocations_usd": candidate.get("allocations_usd") if candidate else {},
            "trigger_symbols": candidate.get("trigger_symbols") if candidate else [],
            "extra_cash_usd": advice.get("extra_cash"),
        }
        checks.extend([{
            "name": "投资建议行情",
            "status": "pass" if advice_ready else "fail",
            "detail": "投资建议与本次市场快照使用同一完整交易日" if advice_ready else "投资建议需要重新评估",
        }, {
            "name": "机会档位",
            "status": "pass" if candidate else "warn",
            "detail": f"当前候选为第 {candidate.get('stage')} 档" if candidate else "当前没有待执行候选档位",
        }, {
            "name": "风险复核与批准",
            "status": "pass" if action_status == "approved" and risk_complete else "warn",
            "detail": (
                f"已批准，美元上限 ${approved:,.2f}" if action_status == "approved" and approved is not None
                else "尚未完成并批准风险复核"
            ),
        }, {
            "name": "补充条件",
            "status": "pass" if supplemental_ok else "fail",
            "detail": "已启用条件仍满足" if supplemental_ok else "条件未满足或资料不足",
        }])
        if not advice_ready or not market_fresh:
            confirmation = "opportunity_data_refresh_required"
            headline = "市场数据不足，无法完成下单前核对"
            summary = "请返回投资建议更新行情并重新评估，暂不要按旧金额操作。"
        elif not candidate:
            confirmation = "opportunity_no_candidate"
            headline = "当前没有需要下单前核对的机会资金档位"
            summary = "机会资金继续等待；具体档位、金额和配置以投资建议页为准。"
        elif not supplemental_ok:
            confirmation = "opportunity_changed_reassess"
            headline = "原投资建议需要重新核对"
            summary = "已启用的补充条件未满足或资料不足，请返回投资建议重新评估。"
        elif action_status != "approved" or not risk_complete:
            confirmation = "opportunity_risk_review_required"
            headline = "先完成机会资金风险复核"
            summary = "当前档位仍由投资建议管理；完成风险复核和批准金额后，再执行下单前核对。"
        elif approved is None or approved <= 0 or executable is None or executable <= 0:
            confirmation = "opportunity_awaiting_cash"
            headline = "机会档位已复核，当前没有可执行美元金额"
            summary = "请核对额外美元到账和批准上限，然后返回投资建议重新确认金额。"
        elif not macro_complete:
            confirmation = "opportunity_data_refresh_required"
            headline = "市场环境资料不完整"
            summary = "档位和批准状态仍可查看，但暂不能完成机会资金下单前核对。"
        elif attention:
            confirmation = "opportunity_market_attention"
            headline = "投资建议仍存在，但市场环境需要再次核实"
            summary = "请阅读本页风险变化，并返回投资建议确认原风险复核仍适用。"
        else:
            confirmation = "opportunity_ready_for_final_check"
            headline = "最新市场数据未使原投资建议失效"
            summary = "可按投资建议中的已批准金额进入 Schwab 下单核对；本页不代表实时成交价或自动买入。"
        subject_key = (
            f"opportunity:{advice.get('cycle_key') or 'none'}:"
            f"{candidate.get('stage') if candidate else 'none'}:"
            f"{candidate.get('action_id') if candidate else 'none'}"
        )

    underlying_confirmation = confirmation
    if preview:
        headline = "本地数据预览：" + headline
        summary = "本次没有联网更新，不能替代实际下单前的最新核对。" + summary
        confirmation = "preview"
    warnings = list(dict.fromkeys(
        str(item) for item in [*(context.get("warnings") or [])] if item
    ))
    return {
        "schema_version": SCHEMA_VERSION,
        "purpose": purpose,
        "purpose_label": "本月固定定投" if purpose == "fixed_dca" else "机会资金交易",
        "subject_key": subject_key,
        "status": "ready" if market_fresh and macro_complete else "partial",
        "confirmation_status": confirmation,
        "underlying_confirmation_status": underlying_confirmation,
        "preview": preview,
        "headline": headline,
        "summary": summary,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "market_as_of": market_as_of,
        "market_lag_business_days": market_lag,
        "source": "Yahoo Finance 最近完整美股交易日日收盘数据；正式下单价格以 Schwab 实时报价为准",
        "price_file_sha256": file_sha256(price_csv),
        "context_file_sha256": file_sha256(context_path),
        "advice_signature": advice_signature(advice),
        "plan_signature": plan_signature(plan_data),
        "rule_hash": advice.get("rule_hash") if isinstance(advice, dict) else None,
        "target": target,
        "symbols": symbols,
        "macro": {"vix": vix, "yield_10y": yield10},
        "checks": checks,
        "attention_items": attention,
        "warnings": warnings,
        "manual_review": [
            "在 Schwab 核对实时报价与买卖价差；本页只使用最近完整交易日数据",
            "核对订单金额、股数和标的，确保没有超过计划或批准上限",
            "机会资金按投资建议的 VOO / QQQM 金额执行；固定定投按当月计划执行",
            "实际成交后再回到本工具记录真实成交，不要把计划金额提前入账",
        ],
        "technical_note": "均线、RSI 和波动率仅用于描述环境，不生成新的投入金额或档位。",
    }
