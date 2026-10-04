#!/usr/bin/env python3
"""Local browser forms for the existing VOO/QQQM SQLite investment ledger."""

from __future__ import annotations

import argparse
import csv
import fcntl
import io
import json
import secrets
import sqlite3
import subprocess
import sys
import threading
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from http.cookies import CookieError, SimpleCookie
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.dont_write_bytecode = True
import investment_tool as ledger
import market_analysis
import morning_briefing


def number(value: Decimal | None) -> str | None:
    return ledger.decimal_text(value) if value is not None else None


def public_config(config: dict) -> dict:
    result = {
        "active_plan": "independent_va_only",
        "budget_day": config["budget_day"],
        "backtest_execution_day": config["backtest_execution_day"],
        "generated_report_keep_count": config["generated_report_keep_count"],
        "investment_mode": "va",
        "va_plan": json_value(config["va_plan"]),
    }
    return result


def json_value(value: object) -> object:
    """Convert nested Decimal values without losing their exact text form."""
    if isinstance(value, Decimal):
        return number(value)
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


def file_time(path: Path) -> float:
    return path.stat().st_mtime


def va_backtest_report(config: dict) -> Path | None:
    directory = config["va_backtest_output_dir"]
    reports = sorted(directory.glob("va_backtest_analysis_*.html"))
    return reports[-1] if reports else None


def backtest_status(config: dict, script_dir: Path) -> dict:
    from market_prices import resolve
    source = resolve(script_dir / "data" / "source_adjusted_close.csv")
    last_date = None
    if source is not None and source.is_file():
        with source.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                last_date = row.get("Date") or last_date
    va_report = va_backtest_report(config)
    return {
        "source_last_date": last_date,
        "source_mtime": file_time(source) if source is not None and source.is_file() else None,
        "va_report_available": va_report is not None,
        "va_report_last_date": (va_report.stem.rsplit("_", 1)[-1] if va_report else None),
        "va_report_mtime": (file_time(va_report) if va_report else None),
    }


def market_reference(state: dict, *, evaluation_date: date) -> dict:
    """Use the same estimate prices and dates shown in the holdings table."""
    held = []
    for symbol in ledger.SYMBOLS:
        position = state["positions"][symbol]
        if position.shares <= 0:
            continue
        valuation = state["valuations"][symbol]
        if valuation["price"] is None or not valuation["date"]:
            return {"value": None, "as_of": None, "as_of_display": None,
                    "source_display": None,
                    "warning": f"{symbol} 尚无估算用价格，请先在持仓页更新参考报价。"}
        held.append((symbol, valuation))
    if not held:
        return {"value": "0", "as_of": None, "as_of_display": "暂无持仓",
                "source_display": "暂无持仓", "warning": ""}

    dates = {valuation["date"] for _, valuation in held}
    source_keys = {valuation["source_key"] for _, valuation in held}
    as_of = next(iter(dates)) if len(dates) == 1 else None
    as_of_display = (as_of if as_of else
                     " · ".join(f"{symbol} {valuation['date']}" for symbol, valuation in held))
    warnings = []
    if source_keys == {"market"}:
        source_display = f"按 {ledger.QUOTE_SOURCE_LABELS['market']}"
    elif source_keys == {"manual"}:
        source_display = "按手动更新的参考报价"
    elif "trade" in source_keys:
        source_display = "各标的按持仓表所示的最新可用价格"
        warnings.append("部分标的仍使用最近成交价，请更新行情或按需手动更新参考报价。")
    else:
        source_display = "各标的按持仓表所示的最新可用价格"
    oldest_age = max((evaluation_date - date.fromisoformat(valuation["date"])).days
                     for _, valuation in held)
    if oldest_age > 7:
        warnings.append(f"最早估算用价格距今天已 {oldest_age} 天，相关市值和盈亏可能过时。")
    return {"value": number(state["market_value"]), "as_of": as_of,
            "as_of_display": as_of_display, "source_display": source_display,
            "warning": " ".join(warnings), "age_days": oldest_age}


def add_months(month: str, offset: int) -> str:
    """Return YYYY-MM shifted by offset months."""
    year, value = map(int, month.split("-"))
    absolute = year * 12 + value - 1 + offset
    return f"{absolute // 12:04d}-{absolute % 12 + 1:02d}"


def investment_calendar(
    config: dict,
    con: sqlite3.Connection,
    transactions: list[sqlite3.Row],
    state: dict,
    *,
    snapshot_date: date,
    months: int = 12,
) -> dict:
    """Build a rolling VA calendar backed by frozen monthly checkpoints."""
    current_month = snapshot_date.strftime("%Y-%m")
    start_month = config["va_plan"]["start_month"]
    anchor_month = max(start_month, current_month)
    budget_day = int(config["budget_day"])
    checkpoints = ledger.va_calendar_checkpoints(con)
    checkpoint_months = {month for month, _symbol in checkpoints}
    history_start_month = min({start_month, *checkpoint_months})
    activity = ledger.va_fixed_buy_activity(transactions)
    cent = Decimal("0.01")

    def status_for_checkpoint(month: str, required: Decimal, actual: Decimal) -> str:
        if actual > required + ledger.va_over_budget_tolerance(required):
            return "over_budget"
        if required <= Decimal("0.005"):
            return "completed" if actual > 0 else "no_buy_needed"
        if actual + Decimal("0.02") >= required:
            return "completed"
        if actual > 0:
            return "partial" if month == current_month else "incomplete"
        return "incomplete" if month < current_month else "ready_to_execute"

    def symbol_payload(month: str, symbol: str, target: Decimal) -> dict:
        checkpoint = checkpoints.get((month, symbol))
        execution = activity.get((month, symbol))
        actual = (execution or {}).get("amount", Decimal(0)).quantize(
            cent, rounding=ROUND_HALF_UP
        )
        fees = (execution or {}).get("fees", Decimal(0)).quantize(
            cent, rounding=ROUND_HALF_UP
        )
        shares = (execution or {}).get("shares", Decimal(0))
        if checkpoint is not None:
            target = ledger.D(checkpoint["target_value_usd"])
            required = ledger.D(checkpoint["required_buy_usd"])
            remaining = max(Decimal(0), required - actual).quantize(
                cent, rounding=ROUND_HALF_UP
            )
            status = status_for_checkpoint(month, required, actual)
            progress = (Decimal(100) if required <= Decimal("0.005") else
                        min(Decimal(100), actual / required * 100))
            return {
                "status": status,
                "confirmed": True,
                "provisional": False,
                "target_value_usd": number(target),
                "required_buy_usd": number(required),
                "actual_buy_usd": number(actual),
                "actual_fee_usd": number(fees),
                "actual_shares": number(shares),
                "remaining_usd": number(remaining),
                "completion_pct": number(progress),
                "reference_market_value_usd": checkpoint["reference_market_value_usd"],
                "reference_shares": checkpoint["reference_shares"],
                "reference_price_usd": checkpoint["reference_price_usd"],
                "reference_price_date": checkpoint["reference_price_date"],
                "reference_price_source": checkpoint["reference_price_source"],
                "budget_date": (
                    checkpoint["budget_date"]
                    if "budget_date" in checkpoint.keys() and checkpoint["budget_date"]
                    else ledger.va_budget_date(config, month).isoformat()
                ),
                "evaluated_on": checkpoint["evaluated_on"],
                "completed_on": (
                    checkpoint["evaluated_on"] if required <= Decimal("0.005")
                    else ledger.va_completion_date(execution, required)
                ),
                "freeze_reason": checkpoint["freeze_reason"],
                "note": checkpoint["note"],
                "overage_usd": number(max(Decimal(0), actual - required)),
            }

        is_live_preview = (
            month == current_month and current_month >= start_month
        ) or (
            current_month < start_month and month == start_month
        )
        required = remaining = progress = None
        valuation = state["valuations"][symbol]
        position = state["positions"][symbol]
        if is_live_preview:
            required = max(Decimal(0), target - valuation["value"]).quantize(
                cent, rounding=ROUND_HALF_UP
            )
            remaining = max(Decimal(0), required - actual).quantize(
                cent, rounding=ROUND_HALF_UP
            )
            progress = (Decimal(100) if required <= Decimal("0.005") else
                        min(Decimal(100), actual / required * 100))
        month_budget_date = ledger.va_budget_date(config, month)
        if month > current_month or current_month < start_month:
            status = "future" if actual == 0 else "unconfirmed"
        elif month == current_month:
            status = (
                "waiting_budget_day" if snapshot_date < month_budget_date
                else "awaiting_budget_lock"
            )
        else:
            status = "budget_not_locked"
        return {
            "status": status,
            "confirmed": False,
            "provisional": is_live_preview,
            "target_value_usd": number(target),
            "required_buy_usd": number(required),
            "actual_buy_usd": number(actual),
            "actual_fee_usd": number(fees),
            "actual_shares": number(shares),
            "remaining_usd": number(remaining),
            "completion_pct": number(progress),
            "reference_market_value_usd": (
                number(valuation["value"]) if is_live_preview else None
            ),
            "reference_shares": number(position.shares) if is_live_preview else None,
            "reference_price_usd": number(valuation["price"]) if is_live_preview else None,
            "reference_price_date": valuation["date"] if is_live_preview else None,
            "reference_price_source": valuation["source"] if is_live_preview else None,
            "budget_date": month_budget_date.isoformat(),
            "evaluated_on": None,
            "completed_on": None,
            "freeze_reason": None,
            "note": "预算尚未锁定；当前预估缺口会随行情变化。" if is_live_preview else "",
            "overage_usd": "0",
        }

    def build_row(month: str) -> dict:
        configured_targets = ledger.va_targets_for_month(config, month)
        symbols = {
            symbol: symbol_payload(month, symbol, configured_targets[symbol])
            for symbol in ledger.SYMBOLS
        }
        targets = {
            symbol: ledger.D(symbols[symbol]["target_value_usd"])
            for symbol in ledger.SYMBOLS
        }
        target_total = sum(targets.values(), Decimal(0))
        required_values = [
            ledger.D(symbols[symbol]["required_buy_usd"])
            for symbol in ledger.SYMBOLS
            if symbols[symbol]["required_buy_usd"] is not None
        ]
        actual_total = sum(
            (ledger.D(symbols[symbol]["actual_buy_usd"]) for symbol in ledger.SYMBOLS),
            Decimal(0),
        )
        remaining_values = [
            ledger.D(symbols[symbol]["remaining_usd"])
            for symbol in ledger.SYMBOLS
            if symbols[symbol]["remaining_usd"] is not None
        ]
        symbol_statuses = [symbols[symbol]["status"] for symbol in ledger.SYMBOLS]
        confirmed = all(symbols[symbol]["confirmed"] for symbol in ledger.SYMBOLS)
        if month > current_month or current_month < start_month:
            overall = "future" if actual_total == 0 else "unconfirmed"
        elif confirmed:
            terminal = {"completed", "no_buy_needed"}
            if all(value in terminal for value in symbol_statuses):
                overall = ("no_buy_needed" if all(
                    value == "no_buy_needed" for value in symbol_statuses
                ) else "completed")
            elif any(value == "over_budget" for value in symbol_statuses):
                overall = "over_budget"
            elif any(value in terminal | {"partial"} for value in symbol_statuses):
                overall = "incomplete" if month < current_month else "partial"
            else:
                overall = "incomplete" if month < current_month else "pending"
        elif month < current_month:
            overall = "budget_not_locked"
        else:
            month_budget_date = ledger.va_budget_date(config, month)
            overall = (
                "waiting_budget_day" if snapshot_date < month_budget_date
                else "awaiting_budget_lock"
            )
        required_total = (
            sum(required_values, Decimal(0)) if len(required_values) == len(ledger.SYMBOLS)
            else None
        )
        remaining_total = (
            sum(remaining_values, Decimal(0)) if len(remaining_values) == len(ledger.SYMBOLS)
            else None
        )
        completion = None
        if required_total is not None:
            completion = (Decimal(100) if required_total <= Decimal("0.005") else
                          min(Decimal(100), actual_total / required_total * 100))
        budget_date = (symbols[ledger.SYMBOLS[0]]["budget_date"] if confirmed
                       else ledger.va_budget_date(config, month).isoformat())
        return {
            "month": month,
            "budget_date": budget_date,
            "target_date": budget_date,
            "is_current": month == current_month,
            "is_plan_start": month == start_month,
            "is_confirmed": confirmed,
            "confirmed": confirmed,
            "can_confirm": bool(
                month == current_month and current_month >= start_month and not confirmed
                and snapshot_date >= ledger.va_budget_date(config, month)
            ),
            "is_overdue": month < current_month
                          and overall not in {"completed", "no_buy_needed"},
            "status": overall,
            "targets_usd": {
                "VOO": number(targets["VOO"]),
                "QQQM": number(targets["QQQM"]),
                "total": number(target_total),
            },
            "required_buy_usd": number(required_total),
            "actual_buy_usd": number(actual_total),
            "remaining_usd": number(remaining_total),
            "completion_pct": number(completion),
            "symbols": symbols,
        }

    rolling_rows = [build_row(add_months(anchor_month, offset)) for offset in range(months)]
    history_rows = []
    if current_month > history_start_month:
        month = add_months(current_month, -1)
        while month >= history_start_month:
            history_rows.append(build_row(month))
            month = add_months(month, -1)
    current_row = rolling_rows[0] if rolling_rows else None
    eligible_rows = list(reversed(history_rows))
    if current_month >= start_month and current_row is not None:
        eligible_rows.append(current_row)
    completed_months = sum(
        row["status"] in {"completed", "no_buy_needed"} for row in eligible_rows
    )
    return {
        "start_month": start_month,
        "current_month": current_month,
        "anchor_month": anchor_month,
        "budget_day": budget_day,
        "months_shown": months,
        "monthly_steps_usd": json_value(config["va_plan"]["monthly_steps_usd"]),
        "summary": {
            "current_status": current_row["status"] if current_row else "future",
            "completed_months": completed_months,
            "eligible_months": len(eligible_rows),
            "total_months": len(eligible_rows),
            "history_count": len(history_rows),
            "plan_active": current_month >= start_month,
        },
        "can_confirm_current": bool(
            current_month >= start_month and current_row and not current_row["is_confirmed"]
            and snapshot_date >= ledger.va_budget_date(config, current_month)
        ),
        "current_row": current_row,
        "rolling_rows": rolling_rows,
        "history_rows": history_rows,
        # Keep the original field for older cached frontends.
        "rows": rolling_rows,
    }


def snapshot(config: dict, script_dir: Path) -> dict:
    """Build the local UI state for the single active independent-VA plan."""
    snapshot_date = date.today()
    con = ledger.connect(config["database_path"])
    try:
        transactions = ledger.rows(con)
        state = ledger.calculate(transactions, ledger.get_quotes(con))
        va = ledger.va_summary(state, config, today=snapshot_date)
        target_weights = ledger.va_target_weights(va)
        market_value = state["market_value"]
        holdings = []
        for symbol in ledger.SYMBOLS:
            position, value = state["positions"][symbol], state["valuations"][symbol]
            allocation = value["value"] / market_value * 100 if market_value else Decimal(0)
            target = target_weights[symbol]
            holdings.append({
                "symbol": symbol, "shares": number(position.shares),
                "avg_cost": number(position.basis / position.shares) if position.shares else "0",
                "basis": number(position.basis),
                "price": number(value["price"]), "price_date": value["date"],
                "price_source": value["source"], "value": number(value["value"]),
                "unrealized": number(value["unrealized"]),
                "realized": number(position.realized + position.dividends),
                "allocation_pct": number(allocation), "target_pct": number(target),
                "drift_pct_points": number(allocation - target),
            })
        total_basis = sum(
            (state["positions"][symbol].basis for symbol in ledger.SYMBOLS), Decimal(0)
        )
        holdings_total = {
            "basis": number(total_basis), "value": number(state["market_value"]),
            "unrealized": number(state["unrealized"]),
            "allocation_pct": "100" if state["market_value"] else "0",
            "target_pct": "100", "drift_pct_points": "0",
        }
        dividend_rows = [row for row in reversed(transactions) if row["kind"] == "dividend"]
        latest_dividend = {symbol: None for symbol in ledger.SYMBOLS}
        for row in dividend_rows:
            if latest_dividend[row["symbol"]] is None:
                latest_dividend[row["symbol"]] = row
        dividend_by_symbol = {}
        for symbol in ledger.SYMBOLS:
            position = state["positions"][symbol]
            latest = latest_dividend[symbol]
            dividend_by_symbol[symbol] = {
                "gross": number(position.dividend_gross),
                "tax": number(position.dividend_tax),
                "net": number(position.dividends),
                "count": position.dividend_count,
                "last_date": latest["date"] if latest else None,
                "last_gross": number(ledger.D(latest["amount"])) if latest else None,
                "last_tax": number(ledger.D(latest["fee"])) if latest else None,
                "last_net": number(
                    ledger.D(latest["amount"]) - ledger.D(latest["fee"])
                ) if latest else None,
            }
        dividend_summary = {
            "gross": number(state["dividend_gross"]),
            "tax": number(state["dividend_tax"]),
            "net": number(state["dividends"]),
            "count": state["dividend_count"],
            "by_symbol": dividend_by_symbol,
        }
        calendar = investment_calendar(
            config, con, transactions, state, snapshot_date=snapshot_date
        )
        execution = ledger.va_execution_summary(
            con, config, transactions=transactions, state=state, today=snapshot_date
        )
        plan_symbols = {}
        for symbol in ledger.SYMBOLS:
            plan_symbols[symbol] = {
                **json_value(va["symbols"][symbol]),
                **json_value(execution["symbols"][symbol]),
            }
        plan_payload = {
            "mode": "independent_va",
            "month": va["month"],
            "current_month": va["current_month"],
            "start_month": va["start_month"],
            "preview": va["preview"],
            "budget_day": config["budget_day"],
            "budget_date": execution["budget_date"],
            "budget_locked": execution["confirmed"],
            "budget_status": execution["status"],
            "budget_locked_on": execution["locked_on"],
            "locked_budget_usd": number(execution["locked_budget_usd"]),
            "actual_buy_usd": number(execution["actual_buy_usd"]),
            "remaining_budget_usd": number(execution["remaining_usd"]),
            "completion_pct": number(execution["completion_pct"]),
            "live_gap_total_usd": number(execution["live_gap_usd"]),
            "suggested_total_usd": number(
                execution["remaining_usd"]
                if execution["confirmed"] else execution["live_gap_usd"]
            ),
            "raw_required_usd": number(va["raw_required_usd"]),
            "available_cash_usd": number(state["cash"]),
            "symbols": plan_symbols,
        }
        from market_prices import resolve
        price_path = resolve(script_dir / "data" / "source_adjusted_close.csv") or script_dir / "data" / "source_adjusted_close.csv"
        context_path = script_dir / "data" / "market_context.json"
        fields = ("id",) + ledger.CSV_FIELDS
        return {
            "ok": True,
            "as_of_date": snapshot_date.isoformat(),
            "summary": {key: number(state[key]) for key in (
                "cash", "deposits", "withdrawals", "net_contributed", "buys",
                "market_value", "account_value", "realized_sales", "dividend_gross",
                "dividend_tax", "dividends",
                "unrealized", "funding_fees", "total_pnl")}
                       | {"realized": number(state["realized_total"]),
                          "current_basis": number(total_basis)},
            "dividend_summary": dividend_summary,
            "plan": plan_payload,
            "va": json_value(va),
            "investment_calendar": calendar,
            "pending_stock_splits": ledger.pending_stock_splits(con, config),
            "stock_splits": [dict(r) for r in transactions.splits],
            "budget_reference_warnings": ledger.va_budget_reference_warnings(con, transactions),
            "va_execution": json_value(execution),
            "holdings": holdings,
            "holdings_total": holdings_total,
            "transactions": [{key: row[key] for key in fields} for row in reversed(transactions)],
            "config": public_config(config),
            "backtest": backtest_status(config, script_dir),
            "generated_report_storage": ledger.generated_report_storage(config),
            "market_update": load_market_update(script_dir),
            "market_reference": market_reference(
                state, evaluation_date=snapshot_date
            ),
            "morning_briefing": morning_briefing.load_briefing(
                script_dir / "data" / "morning_briefing.json",
                current_market_as_of=max(
                    (item.get("reference_price_date") or "" for item in va["symbols"].values()),
                    default="",
                ) or None,
                current_price_sha256=market_analysis.file_sha256(price_path),
                current_context_sha256=market_analysis.file_sha256(context_path),
                current_advice_signature=None,
                current_plan_signature=market_analysis.plan_signature(plan_payload),
            ),
        }
    finally:
        con.close()

def complete_trade(record: dict) -> None:
    if record["kind"] not in ("buy", "sell"):
        return
    raw = [record.get(key) for key in ("amount", "price", "shares")]
    if sum(value not in (None, "") for value in raw) < 2:
        raise ValueError("买卖需要填写金额、成交价、股数中的至少两项。")
    amount, price, shares = (ledger.D(value) if value not in (None, "") else None for value in raw)
    if any(value <= 0 for value in (amount, price, shares) if value is not None):
        raise ValueError("成交金额、价格和股数须大于零。")
    if amount is None:
        amount = (price * shares).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    elif price is None:
        price = (amount / shares).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    elif shares is None:
        shares = (amount / price).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    record.update(amount=number(amount), price=number(price), shares=number(shares))


def clean_record(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("流水内容格式不正确。")
    kind = str(data.get("kind") or "")
    source = "fixed"  # Internal compatibility value; the only active pool is VA.
    if kind not in ledger.KINDS:
        raise ValueError("请选择有效的流水类型。")
    record = {
        "date": data.get("date"), "kind": kind, "source_type": source,
        "amount": data.get("amount"), "fee": data.get("fee") or "0",
        "note": data.get("note") or "",
        "symbol": data.get("symbol") if kind in ("buy", "sell", "dividend") else None,
        "price": data.get("price") if kind in ("buy", "sell") else None,
        "shares": data.get("shares") if kind in ("buy", "sell") else None,
        "plan_month": data.get("plan_month") if kind in ("deposit", "buy") else None,
        "trigger_note": "",
        "transfer_date": data.get("transfer_date") if kind == "deposit" else None,
        "hkd_sent": data.get("hkd_sent") if kind == "deposit" else None,
        "fx_rate_hkd_per_usd": data.get("fx_rate_hkd_per_usd") if kind == "deposit" else None,
        "fx_cost_hkd": data.get("fx_cost_hkd") if kind == "deposit" else None,
    }
    complete_trade(record)
    ledger.validate_record(record)
    return record


def refresh_after_save(con: sqlite3.Connection, config: dict) -> str:
    try:
        ledger.refresh(con, config)
        return "已保存，并更新报告与 Obsidian。"
    except (ValueError, OSError, UnicodeError) as exc:
        return f"流水已保存；报告或 Obsidian 更新失败：{exc}"


def load_market_update(root: Path) -> dict:
    path = root / "data" / "market_update_status.json"
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_market_update(root: Path, *, ok: bool, message: str, quote_sync: dict | None = None) -> None:
    previous = load_market_update(root)
    value = {"ok": ok, "attempted_at": datetime.now().astimezone().isoformat(),
             "message": message, "last_success_at": previous.get("last_success_at")}
    if ok:
        value["last_success_at"] = value["attempted_at"]
        value["quotes"] = quote_sync or {}
    else:
        value["quotes"] = previous.get("quotes", {})
    ledger.atomic_text(root / "data" / "market_update_status.json",
                       json.dumps(json_value(value), ensure_ascii=False, indent=2))


def apply_refreshed_market(config: dict, root: Path, result: dict) -> tuple[dict, str | None]:
    """Sync validated ETF closes to valuation and refresh real-investment outputs."""
    info = result["refresh_info"]
    con = ledger.connect(config["database_path"])
    report_warning = None
    try:
        if ledger.pending_stock_splits(con,config):
            raise ValueError("检测到持仓拆股，请先在投资日历核对拆股，再更新持仓报价。")
        quote_sync = ledger.sync_market_quotes(
            con,
            root / "data" / "source_adjusted_close.csv",
            expected_as_of=info["latest_date"],
        )
        try:
            ledger.refresh(con, config)
        except (ValueError, OSError, UnicodeError, sqlite3.Error) as exc:
            report_warning = str(exc)
    finally:
        con.close()
    details = [f"行情截至 {quote_sync['as_of']}"]
    for key, label in (("updated", "报价已更新"), ("retained", "保留日期较新的报价"), ("unchanged", "报价已是最新")):
        if quote_sync.get(key):
            details.append("/".join(quote_sync[key]) + " · " + label)
    if report_warning:
        details.append("报告同步提示：" + report_warning)
    save_market_update(root, ok=True, message="；".join(details), quote_sync=quote_sync)
    return quote_sync, report_warning


def refreshed_market_message(result: dict, quote_sync: dict, report_warning: str | None,
                             *, backtest: bool) -> str:
    info = result["refresh_info"]
    actual_date = result.get("last_date") or info["latest_date"]
    if info["latest_date"] < info["cutoff_date"]:
        prefix = ("回测已完成" if backtest else "市场数据已更新")
        message = (f"{prefix}；Yahoo 尚未提供 {info['cutoff_date']} 的有效收盘价，"
                   f"本次使用截至 {actual_date} 的最后有效行情")
    else:
        message = (f"已联网获取行情并完成回测，数据截至 {actual_date}" if backtest else
                   f"已联网更新市场数据，数据截至 {actual_date}")
    if quote_sync["updated"]:
        message += (f"；{'/'.join(quote_sync['updated'])} 的持仓估算参考价已同步为 "
                    f"{quote_sync['as_of']} 的 Yahoo Finance 实盘收盘价")
    if quote_sync["retained"]:
        message += (f"；{'/'.join(quote_sync['retained'])} 已有日期更晚的参考报价，"
                    "因此保留原值")
    message += "；持仓估值已按各标的最新可用日期重新计算"
    if report_warning:
        message += f"；实盘报告或 Obsidian 更新失败：{report_warning}"
    if result.get("advice_warning"):
        message += f"；晨报所需市场环境数据未更新：{result['advice_warning']}"
    elif result.get("advice_context", {}).get("warnings"):
        message += "；部分市场环境数据沿用缓存，请查看晨报来源提示"
    else:
        message += "；VIX 与美国 10Y 等市场环境数据已核对"
    return message + "。"


def validate_config_file(config_file: Path, raw: dict) -> dict:
    candidate = config_file.with_name(config_file.name + ".gui-check")
    try:
        ledger.atomic_text(candidate, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
        return ledger.load_config(candidate)
    finally:
        candidate.unlink(missing_ok=True)




class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], config_file: Path):
        super().__init__(address, Handler)
        self.config_file = config_file
        self.config = ledger.load_config(config_file)
        self.token = secrets.token_urlsafe(32)
        self.root = Path(__file__).resolve().parent
        self.lock = threading.RLock()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    server: LocalServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def authorized(self, query: dict[str, list[str]], *, post: bool = False) -> bool:
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
            return False
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except CookieError:
            cookies = SimpleCookie()
        session_cookie = cookies.get("voo_qqqm_session")
        supplied = (
            self.headers.get("X-Session-Token")
            or query.get("token", [""])[0]
            or (session_cookie.value if session_cookie else "")
        )
        if not supplied or not secrets.compare_digest(supplied, self.server.token):
            return False
        if post and self.headers.get("Origin") not in (None, self.server.origin):
            return False
        return True

    def reply(self, status: int, payload: bytes, content_type: str,
              disposition: str | None = None,
              extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def reply_json(self, status: int, obj: dict) -> None:
        self.reply(status, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        if not self.authorized(query):
            self.reply_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "本地会话无效，请重新打开启动脚本。"})
            return
        try:
            if parsed.path == "/":
                template = (self.server.root / "investment_gui.html").read_text(encoding="utf-8")
                page = template.replace("__TOKEN__", self.server.token)
                self.reply(
                    HTTPStatus.OK,
                    page.encode("utf-8"),
                    "text/html; charset=utf-8",
                    extra_headers={
                        "Set-Cookie": (
                            f"voo_qqqm_session={self.server.token}; "
                            "Path=/; SameSite=Strict; HttpOnly"
                        )
                    },
                )
            elif parsed.path == "/api/calendar/preview":
                with self.server.lock:
                    con = ledger.connect(self.server.config["database_path"])
                    try:
                        preview = ledger.va_budget_preview(con, self.server.config, query.get("plan_month", [date.today().strftime("%Y-%m")])[0])
                    finally:
                        con.close()
                    self.reply_json(HTTPStatus.OK, {"ok": True, "preview": preview})
            elif parsed.path == "/api/health":
                import program_health
                with self.server.lock:
                    self.reply_json(HTTPStatus.OK,{"ok":True,"checks":program_health.inspect(self.server.config,self.server.root)})
            elif parsed.path == "/download/stock-splits":
                with self.server.lock:
                    con=ledger.connect(self.server.config['database_path'])
                    try:
                        output=io.StringIO();writer=csv.DictWriter(output,fieldnames=('id','symbol','date','ratio','note','created_at'));writer.writeheader()
                        for row in con.execute('SELECT * FROM stock_splits ORDER BY date,id'):writer.writerow(dict(row))
                    finally:con.close()
                    self.reply(HTTPStatus.OK,('﻿'+output.getvalue()).encode(),'text/csv; charset=utf-8','attachment; filename="voo_qqqm_stock_splits.csv"')
            elif parsed.path == "/api/state":
                with self.server.lock:
                    self.reply_json(HTTPStatus.OK, snapshot(self.server.config, self.server.root))
            elif parsed.path == "/report":
                with self.server.lock:
                    con = ledger.connect(self.server.config["database_path"])
                    try:
                        ledger.refresh(con, {**self.server.config, "obsidian_sync_enabled": False})
                    finally:
                        con.close()
                    self.reply(HTTPStatus.OK, self.server.config["report_path"].read_bytes(),
                               "text/html; charset=utf-8")
            elif parsed.path == "/download/csv":
                with self.server.lock:
                    con = ledger.connect(self.server.config["database_path"])
                    try:
                        output = io.StringIO()
                        writer = csv.DictWriter(output, fieldnames=("id",) + ledger.CSV_FIELDS)
                        writer.writeheader()
                        for row in ledger.rows(con):
                            writer.writerow({key: row[key] for key in ("id",) + ledger.CSV_FIELDS})
                    finally:
                        con.close()
                data = ("\ufeff" + output.getvalue()).encode("utf-8")
                self.reply(HTTPStatus.OK, data, "text/csv; charset=utf-8",
                           'attachment; filename="voo_qqqm_transactions.csv"')
            elif parsed.path == "/backtest/report":
                self.reply_json(
                    HTTPStatus.GONE,
                    {"ok": False, "error": "旧配置比例回测已停用；请打开独立 VA 对比报告。"},
                )
            elif parsed.path == "/va-backtest/report":
                with self.server.lock:
                    report = va_backtest_report(self.server.config)
                    if report is None:
                        self.reply_json(
                            HTTPStatus.NOT_FOUND,
                            {"ok": False, "error": "还没有生成 VA 对比回测报告。"},
                        )
                    else:
                        self.reply(
                            HTTPStatus.OK, report.read_bytes(), "text/html; charset=utf-8"
                        )
            elif parsed.path == "/opportunity-backtest/report":
                self.reply_json(
                    HTTPStatus.GONE,
                    {"ok": False, "error": "旧机会资金回测已停用；当前唯一计划是独立 VA。"},
                )
            elif parsed.path == "/morning-briefing/report":
                with self.server.lock:
                    report = self.server.root / "reports" / "美股深度晨报.html"
                    if not report.is_file():
                        self.reply_json(
                            HTTPStatus.NOT_FOUND,
                            {"ok": False, "error": "还没有生成美股深度晨报。"},
                        )
                    else:
                        briefing = (snapshot(self.server.config, self.server.root)
                                    .get("morning_briefing", {}))
                        if (briefing.get("status") != "complete"
                                or briefing.get("stale")):
                            self.reply_json(
                                HTTPStatus.CONFLICT,
                                {"ok": False, "error": "这份美股晨报无法验证或已过期，请先重新生成。"},
                            )
                        else:
                            self.reply(
                                HTTPStatus.OK, report.read_bytes(), "text/html; charset=utf-8"
                            )
            else:
                self.reply_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "页面不存在。"})
        except (ValueError, OSError, sqlite3.Error, UnicodeError) as exc:
            self.reply_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if not self.authorized(parse_qs(parsed.query), post=True):
            self.reply_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "本地会话无效，请重新打开启动脚本。"})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            self.reply_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"ok": False, "error": "只接受 JSON 表单。"})
            return
        data = {}
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 65536:
                raise ValueError("表单内容过大。")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("表单内容格式不正确。")
            with self.server.lock:
                message = self.action(parsed.path, data)
            self.reply_json(HTTPStatus.OK, {"ok": True, "message": message})
        except (ValueError, KeyError, TypeError, ImportError, RuntimeError, sqlite3.Error, OSError, UnicodeError) as exc:
            if parsed.path in ("/api/market-refresh", "/api/morning-briefing", "/api/backtest") and isinstance(data, dict) and data.get("refresh", True):
                try:
                    save_market_update(self.server.root, ok=False, message="本次操作未完成：" + str(exc))
                except OSError:
                    pass
            self.reply_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})

    def action(self, path: str, data: dict) -> str:
        config = self.server.config
        if (path.startswith("/api/opportunity")
                or path in ("/api/market-analysis", "/api/review")):
            raise ValueError("当前只启用独立 VA 计划；旧机会资金与条件档位功能已停用。")
        if path == "/api/market-refresh":
            if data:
                raise ValueError("行情更新不接受交易或计划参数。")
            from backtest import refresh_source_prices
            info = refresh_source_prices(self.server.root / "data/source_adjusted_close.csv")
            quotes, warning = apply_refreshed_market(config, self.server.root, {"refresh_info": info})
            return load_market_update(self.server.root)["message"]
        if path == "/api/backup":
            import program_health
            location=program_health.backup(config,self.server.root)
            return f"完整备份已生成并校验：{location}。最近三份完整备份会保留。"
        if path == "/api/stock-split":
            con=ledger.connect(config['database_path'])
            try:
                ledger.record_stock_split(con,config,str(data.get('symbol') or ''),str(data.get('date') or ''),data.get('ratio'),str(data.get('note') or ''))
                return refresh_after_save(con,config)
            finally:con.close()
        if path in ("/api/calendar/confirm", "/api/calendar/recalculate"):
            unexpected = set(data) - {"plan_month", "fingerprint"}
            if unexpected:
                raise ValueError("锁定月度预算的请求包含不支持的字段。")
            con = ledger.connect(config["database_path"])
            try:
                inserted = ledger.apply_va_budget_proposal(
                    con, config, str(data.get("plan_month") or ""),
                    str(data.get("fingerprint") or ""),
                    recalculate=path.endswith("recalculate"),
                )
                refresh_message = refresh_after_save(con, config)
            finally:
                con.close()
            if inserted:
                return ("本月 VA 预算已重算，原预算和新预算已保存为修订记录。" if path.endswith("recalculate") else "本月 VA 预算已锁定。") + " " + refresh_message
            return "本月 VA 预算此前已经锁定，无需重复操作。"
        if path == "/api/transaction":
            record = clean_record(data.get("record"))
            record_id = data.get("id")
            if record_id not in (None, ""):
                record_id = int(record_id)
                if record_id <= 0:
                    raise ValueError("记录 ID 不正确。")
            else:
                record_id = None
            if data.get("action_id") not in (None, ""):
                raise ValueError("当前仅启用独立 VA 计划，不接受机会档位关联。")
            con = ledger.connect(config["database_path"])
            try:
                ledger.add_or_edit(con, record, record_id, va_config=config)
                return refresh_after_save(con, config)
            finally:
                con.close()
        if path == "/api/delete":
            record_id = int(data["id"])
            if record_id <= 0:
                raise ValueError("记录 ID 不正确。")
            con = ledger.connect(config["database_path"])
            try:
                ledger.delete_record(con, record_id)
                return refresh_after_save(con, config)
            finally:
                con.close()
        if path == "/api/quote":
            con = ledger.connect(config["database_path"])
            try:
                ledger.set_manual_quote(
                    con, data.get("symbol"), data.get("date"), data.get("price")
                )
                return refresh_after_save(con, config)
            finally:
                con.close()
        if path == "/api/config":
            raw = json.loads(self.server.config_file.read_text(encoding="utf-8"))
            raw["active_plan"] = "independent_va_only"
            for key in ("budget_day", "generated_report_keep_count"):
                if key in data:
                    raw[key] = data[key]
            raw.pop("target_day", None)
            raw.setdefault("backtest_execution_day", config["backtest_execution_day"])
            raw["investment_mode"] = "va"
            if "va_plan" in data:
                if not isinstance(data["va_plan"], dict):
                    raise ValueError("VA 计划格式不正确。")
                previous_va = raw.get("va_plan", {})
                if not isinstance(previous_va, dict):
                    previous_va = {}
                incoming_va = data["va_plan"]
                merged_va = {**previous_va, **incoming_va}
                for group_name in ("initial_targets_usd", "monthly_steps_usd"):
                    if group_name in incoming_va:
                        if not isinstance(incoming_va[group_name], dict):
                            raise ValueError(f"VA 计划 {group_name} 格式不正确。")
                        previous_group = previous_va.get(group_name, {})
                        if not isinstance(previous_group, dict):
                            previous_group = {}
                        merged_va[group_name] = {
                            **previous_group, **incoming_va[group_name]
                        }
                # Never persist the retired first-month cost-basis switches.
                merged_va.pop("bootstrap_basis", None)
                merged_va.pop("ongoing_basis", None)
                raw["va_plan"] = merged_va
            raw.pop("opportunity_backtest_cny_per_usd", None)
            if isinstance(raw.get("opportunity_plan"), dict):
                raw["opportunity_plan"].pop("monthly_cny", None)
                raw["opportunity_plan"].pop("cap_cny", None)
            validate_config_file(self.server.config_file, raw)
            ledger.atomic_text(self.server.config_file, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
            self.server.config = ledger.load_config(self.server.config_file)
            con = ledger.connect(self.server.config["database_path"])
            try:
                ledger.refresh(con, self.server.config)
            finally:
                con.close()
            return "配置已保存，报告已更新。"
        if path == "/api/cleanup-generated-reports":
            result = ledger.cleanup_generated_reports(config)
            message = (
                f"生成文件回收完成：删除 {result['removed_count']} 份旧回测报告，"
                f"保留规则为每类最近 {result['keep_count']} 份。"
            )
            if result["warnings"]:
                message += " 部分文件未能处理：" + "；".join(result["warnings"])
            return message
        if path == "/api/sync":
            con = ledger.connect(config["database_path"])
            try:
                ledger.refresh(con, config)
            finally:
                con.close()
            return "HTML 与 Obsidian 已同步。"
        if path == "/api/morning-briefing":
            unexpected = set(data) - {"refresh"}
            if unexpected:
                raise ValueError("美股晨报只接受手动联网生成指令。")
            if data.get("refresh") is not True or type(data.get("refresh")) is not bool:
                raise ValueError("美股晨报只能在手动点击后联网生成。")
            refreshed = ledger.refresh_market_data(self.server.root)
            quote_sync, report_warning = apply_refreshed_market(config, self.server.root, refreshed)
            current = snapshot(config, self.server.root)
            briefing = morning_briefing.generate_briefing(
                self.server.root,
                plan=current.get("plan") or {},
                advice=current.get("advice") or {},
                extra=current.get("extra") or {},
                price_file_sha256=market_analysis.file_sha256(
                    self.server.root / "data" / "source_adjusted_close.csv"
                ),
                context_file_sha256=market_analysis.file_sha256(
                    self.server.root / "data" / "market_context.json"
                ),
                advice_signature=None,
                plan_signature=market_analysis.plan_signature(
                    current.get("plan") or {}
                ),
            )
            source_statuses = briefing.get("source_statuses") or []
            successful = sum(1 for item in source_statuses if item.get("status") == "ok")
            source_total = len(source_statuses)
            message = (
                f"今日美股深度晨报已生成：市场日线截至 {briefing['market_as_of']}，"
                f"收录 {len(briefing.get('news') or [])} 条近期信息，"
                f"外部来源成功 {successful}/{source_total}。"
            )
            if quote_sync.get("updated"):
                message += " 持仓估算用价格已同步为同一份已校验行情。"
            if report_warning:
                message += f" 实盘报告更新提示：{report_warning}。"
            message += " 本次未创建交易，也未改变 VA 目标路径。"
            return message
        if path == "/api/backtest":
            refresh = data.get("refresh", True)
            if type(refresh) is not bool:
                raise ValueError("回测模式须为联网刷新或本地离线。")
            result = ledger.run_backtest(config, self.server.root, refresh_prices=refresh)
            metrics = result.get("metrics") or {}
            va_total = metrics.get("independent_total") or {}
            dca = metrics.get("fixed_dca") or {}
            message = (
                f"独立 VA 回测已完成，行情截至 {result['last_date']}；"
                f"VA 合计期末 ${float(va_total.get('ending_usd', 0)):,.2f}，"
                f"DCA 研究基准期末 ${float(dca.get('ending_usd', 0)):,.2f}。"
            )
            if not refresh:
                return "本地数据" + message
            quote_sync, report_warning = apply_refreshed_market(
                config, self.server.root, result
            )
            return refreshed_market_message(
                result, quote_sync, report_warning, backtest=True
            ) + " " + message
        if path == "/api/shutdown":
            # Let the HTTP response leave the socket before stopping the server;
            # an immediate shutdown can truncate the final JSON in some browsers.
            timer = threading.Timer(0.1, self.server.shutdown)
            timer.daemon = True
            timer.start()
            return "本地窗口服务已关闭。"
        raise ValueError("未知操作。")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true", help="供本地验证使用，不自动打开浏览器")
    args = parser.parse_args()
    config_path = args.config.expanduser().resolve()
    lock_path = config_path.parent / "data" / ".investment_gui.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    instance_lock = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(instance_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        instance_lock.close()
        raise ValueError("投资工具已经在运行，请使用已打开的管理页面。") from exc
    try:
        lock_path.chmod(0o600)
    except OSError:
        pass
    server = LocalServer(("127.0.0.1", args.port), config_path)
    server.instance_lock = instance_lock
    url = f"{server.origin}/?token={server.token}"
    print(f"GUI_URL={url}", flush=True)
    if not args.no_browser:
        try:
            subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            print("请在浏览器打开上方的本地地址。", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(f"无法打开记账窗口：{exc}", file=sys.stderr)
        sys.exit(1)
