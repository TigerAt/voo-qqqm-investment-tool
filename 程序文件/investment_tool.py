#!/usr/bin/env python3
"""VOO / QQQM 本地投资流水、估值、HTML 报告与 Obsidian 同步。

直接运行进入中文菜单；`--report` 只更新实盘报告；`--backtest` 联网刷新后回测，
`--backtest-offline` 使用本地既有行情离线重算。
仅使用 Python 标准库。真实流水保存在 SQLite，回测结果始终位于独立目录。
"""

from __future__ import annotations

import argparse
import csv
import html
import hashlib
import json
import math
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, getcontext
from pathlib import Path

getcontext().prec = 28
SYMBOLS = ("VOO", "QQQM")
KINDS = ("deposit", "withdrawal", "buy", "sell", "dividend")
KIND_ZH = {"deposit": "入金", "withdrawal": "出金", "buy": "买入", "sell": "卖出", "dividend": "分红"}
QUOTE_SOURCE_LABELS = {
    "manual": "手动参考报价",
    "market": "Yahoo Finance 实盘收盘价",
}
DEFAULTS = {
    "active_plan": "independent_va_only",
    "monthly_budget_usd": "300",
    "monthly_budget_schedule": [
        {"effective_month": "2026-10", "budget_usd": "500"},
    ],
    # Real execution: determine and freeze the month's VA budget on this local
    # calendar day, then record any number of purchases through month end.
    "budget_day": 2,
    # Historical research still needs one deterministic execution date.  This
    # is a modelling assumption only and never acts as a real-account deadline.
    "backtest_execution_day": 10,
    "monthly_plan_start": None,
    "target_weights": {"VOO": 62.5, "QQQM": 37.5},
    "investment_mode": "va",
    "va_plan": {
        "start_month": "2026-10",
        "initial_targets_usd": {"VOO": "500", "QQQM": "300"},
        "monthly_steps_usd": {"VOO": "500", "QQQM": "300"},
        "sell_if_above_target": False,
    },
    "database_path": "data/investments.sqlite3",
    "report_path": "reports/真实投资报告.html",
    "backtest_output_dir": "backtest_results",
    "va_backtest_output_dir": "va_backtest_results",
    "generated_report_keep_count": 3,
    "backtest_initial_usd": "0",
    # Historical research is a separate, constant-dollar assumption.  It does
    # not rewrite the effective-dated real contribution plan above.
    "backtest_monthly_budget_usd": "500",
    "opportunity_backtest_output_dir": "opportunity_backtest_results",
    "obsidian_vault_path": "~/Documents/Obsidian/投资理财",
    "obsidian_subdir": "65-投资理财/VOO-QQQM",
    "obsidian_sync_enabled": False,
    "buy_rules": {
        "min_52w_drawdown_pct": None,
        "ma20_position": "any", "ma50_position": "any", "ma200_position": "any",
        "min_vix": None, "min_10y_yield_pct": None, "max_10y_yield_pct": None,
        "require_valuation_note": True, "require_manual_confirmation": True
    },
    "opportunity_plan": {
        "start_month": "2026-10",
        "monthly_usd": "300",
        "cap_usd": "1800",
        "rule_profile": "v1_default",
        "stage_scope": "combined",
        "stage_ratios_pct": [20, 25, 25, 30],
        "excess_spread_months": 6,
        "risk_half_multiplier_pct": 50,
        "require_risk_review": True,
        "reset_rule": "both_symbols_new_252d_high",
    },
}


def D(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("请输入有效数字。") from exc
    if not result.is_finite():
        raise ValueError("金额和价格必须是有限数字。")
    return result


def fmt(value: Decimal | None, places: int = 2) -> str:
    return "待填估值" if value is None else f"{value:,.{places}f}"


def usd(value: Decimal | None, places: int = 2) -> str:
    """Format USD with the sign before the currency symbol."""
    if value is None:
        return "待填估值"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.{places}f}"


def cny(value: Decimal | None, places: int = 2) -> str:
    if value is None:
        return "待填金额"
    sign = "-" if value < 0 else ""
    return f"{sign}¥{abs(value):,.{places}f}"


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _month_text(value: object, field: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", text):
        raise ValueError(f"{field} 须为 YYYY-MM。")
    return text


def monthly_budget_for(config: dict, plan_month: str) -> Decimal:
    """Return the fixed-contribution budget that was effective in one month."""
    month = _month_text(plan_month, "计划月份")
    budget = D(config["monthly_budget_usd"])
    for entry in config.get("monthly_budget_schedule", []):
        if entry["effective_month"] <= month:
            budget = D(entry["budget_usd"])
        else:
            break
    return budget


def parse_day(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        raise ValueError("日期须为 YYYY-MM-DD，例如 2026-09-21。")
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError("日期须为 YYYY-MM-DD，例如 2026-09-21。") from exc
    if parsed > date.today():
        raise ValueError("日期不可晚于今天。")
    return parsed.isoformat()


def resolve_from_config(config_file: Path, raw: str) -> Path:
    path = Path(raw).expanduser()
    return (path if path.is_absolute() else config_file.parent / path).resolve()


def load_config(config_file: Path) -> dict:
    if not config_file.exists():
        config_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_text(config_file, json.dumps(DEFAULTS, ensure_ascii=False, indent=2) + "\n")
        print(f"已创建配置文件：{config_file}")
    with config_file.open(encoding="utf-8") as handle:
        user = json.load(handle)
    try:
        config_file.chmod(0o600)
    except OSError:
        pass
    if not isinstance(user, dict):
        raise ValueError("配置文件顶层须为 JSON 对象。")
    config = {**DEFAULTS, **user}
    config["buy_rules"] = {**DEFAULTS["buy_rules"], **user.get("buy_rules", {})}
    raw_opportunity = user.get("opportunity_plan", {})
    if not isinstance(raw_opportunity, dict):
        raise ValueError("opportunity_plan 须为 JSON 对象。")
    opportunity = {**DEFAULTS["opportunity_plan"], **raw_opportunity}
    config["opportunity_plan"] = opportunity
    raw_va = user.get("va_plan", {})
    if not isinstance(raw_va, dict):
        raise ValueError("va_plan 须为 JSON 对象。")
    va_plan = {**DEFAULTS["va_plan"], **raw_va}
    va_plan["initial_targets_usd"] = {
        **DEFAULTS["va_plan"]["initial_targets_usd"],
        **(raw_va.get("initial_targets_usd") or {}),
    }
    va_plan["monthly_steps_usd"] = {
        **DEFAULTS["va_plan"]["monthly_steps_usd"],
        **(raw_va.get("monthly_steps_usd") or {}),
    }
    # Older configs could opt into a first-month cost-basis bootstrap.  VA now
    # always measures every gap against reference market value, so discard the
    # legacy switches while loading rather than letting them alter calculations.
    va_plan.pop("bootstrap_basis", None)
    va_plan.pop("ongoing_basis", None)
    config["va_plan"] = va_plan
    if config.get("active_plan", "independent_va_only") != "independent_va_only":
        raise ValueError("active_plan 当前仅支持 independent_va_only。")
    # The former fixed-contribution and opportunity modes are retained only as
    # migration data.  Loading an older file can never reactivate them.
    config["active_plan"] = "independent_va_only"
    config["investment_mode"] = "va"
    budget = D(config["monthly_budget_usd"])
    if budget <= 0:
        raise ValueError("monthly_budget_usd 须大于零。")
    schedule_raw = config.get("monthly_budget_schedule", [])
    if not isinstance(schedule_raw, list):
        raise ValueError("monthly_budget_schedule 须为数组。")
    schedule: list[dict[str, str]] = []
    seen_months: set[str] = set()
    for entry in schedule_raw:
        if not isinstance(entry, dict):
            raise ValueError("monthly_budget_schedule 的每一项须为对象。")
        effective_month = _month_text(entry.get("effective_month"), "定投预算生效月份")
        amount = D(entry.get("budget_usd"))
        if amount <= 0:
            raise ValueError("定投预算调度金额须大于零。")
        if effective_month in seen_months:
            raise ValueError(f"定投预算生效月份重复：{effective_month}。")
        seen_months.add(effective_month)
        schedule.append({
            "effective_month": effective_month,
            "budget_usd": decimal_text(amount),
        })
    schedule.sort(key=lambda item: item["effective_month"])
    # Migrate older files where target_day served two unrelated purposes.  The
    # legacy value remains the research execution assumption; the real budget
    # day adopts the new explicit default unless it was already configured.
    legacy_target_day = user.get("target_day")
    if "backtest_execution_day" not in user and legacy_target_day is not None:
        config["backtest_execution_day"] = legacy_target_day
    for key, label in (
        ("budget_day", "budget_day"),
        ("backtest_execution_day", "backtest_execution_day"),
    ):
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 28:
            raise ValueError(f"{label} 须为 1–28 的整数。")
    config.pop("target_day", None)
    keep_count = config["generated_report_keep_count"]
    if (isinstance(keep_count, bool) or not isinstance(keep_count, int)
            or not 1 <= keep_count <= 30):
        raise ValueError("generated_report_keep_count 须为 1–30 的整数。")
    rules = config["buy_rules"]
    for key in ("ma20_position", "ma50_position", "ma200_position"):
        if rules[key] not in ("any", "above", "below"):
            raise ValueError(f"{key} 须为 any、above 或 below。")
    for key in ("min_52w_drawdown_pct", "min_vix", "min_10y_yield_pct", "max_10y_yield_pct"):
        if rules[key] is not None and D(rules[key]) < 0:
            raise ValueError(f"{key} 不可为负。")
    if rules["min_52w_drawdown_pct"] is not None and D(rules["min_52w_drawdown_pct"]) > 100:
        raise ValueError("52 周高点回撤下限不可超过 100%。")
    if (rules["min_10y_yield_pct"] is not None and rules["max_10y_yield_pct"] is not None
            and D(rules["min_10y_yield_pct"]) > D(rules["max_10y_yield_pct"])):
        raise ValueError("10 年期收益率下限不可高于上限。")
    for key in ("require_valuation_note", "require_manual_confirmation"):
        if not isinstance(rules[key], bool):
            raise ValueError(f"{key} 须为 true 或 false。")
    opportunity_start = _month_text(opportunity["start_month"], "机会资金计划起始月份")
    monthly_opportunity = D(opportunity["monthly_usd"])
    cap_opportunity = D(opportunity["cap_usd"])
    if monthly_opportunity <= 0 or cap_opportunity <= 0:
        raise ValueError("机会资金的 monthly_usd 与 cap_usd 须大于零。")
    if monthly_opportunity > cap_opportunity:
        raise ValueError("机会资金每月计划金额不可高于常备上限。")
    if opportunity["rule_profile"] not in ("v1_sensitive", "v1_default", "v1_conservative"):
        raise ValueError("opportunity_plan.rule_profile 须为 v1_sensitive、v1_default 或 v1_conservative。")
    if opportunity["stage_scope"] != "combined":
        raise ValueError("opportunity_plan.stage_scope 当前仅支持 combined。")
    ratios = opportunity["stage_ratios_pct"]
    if not isinstance(ratios, list) or len(ratios) != 4:
        raise ValueError("opportunity_plan.stage_ratios_pct 须包含四个档位比例。")
    ratio_values = [D(value) for value in ratios]
    if any(value <= 0 or value > 100 for value in ratio_values) or sum(ratio_values) != 100:
        raise ValueError("四个机会档位比例须各自大于 0、至多 100，且合计 100%。")
    spread_months = opportunity["excess_spread_months"]
    if isinstance(spread_months, bool) or not isinstance(spread_months, int) or not 3 <= spread_months <= 6:
        raise ValueError("opportunity_plan.excess_spread_months 须为 3–6 的整数。")
    half_multiplier = D(opportunity["risk_half_multiplier_pct"])
    if half_multiplier <= 0 or half_multiplier > 100:
        raise ValueError("opportunity_plan.risk_half_multiplier_pct 须大于 0 且不超过 100。")
    if not isinstance(opportunity["require_risk_review"], bool):
        raise ValueError("opportunity_plan.require_risk_review 须为 true 或 false。")
    if opportunity["reset_rule"] != "both_symbols_new_252d_high":
        raise ValueError("opportunity_plan.reset_rule 当前仅支持 both_symbols_new_252d_high。")
    # Keep this nested object JSON serializable for the local GUI.  Financial
    # calculations convert these canonical decimal strings back through D().
    opportunity.pop("monthly_cny", None)
    opportunity.pop("cap_cny", None)
    opportunity["start_month"] = opportunity_start
    opportunity["monthly_usd"] = decimal_text(monthly_opportunity)
    opportunity["cap_usd"] = decimal_text(cap_opportunity)
    opportunity["stage_ratios_pct"] = [decimal_text(value) for value in ratio_values]
    opportunity["risk_half_multiplier_pct"] = decimal_text(half_multiplier)
    va = config["va_plan"]
    va_start = _month_text(va.get("start_month"), "VA 计划起始月份")
    for group_name in ("initial_targets_usd", "monthly_steps_usd"):
        group = va.get(group_name)
        if not isinstance(group, dict) or set(group) != set(SYMBOLS):
            raise ValueError(f"va_plan.{group_name} 须包含 VOO 和 QQQM。")
        cleaned = {}
        for symbol in SYMBOLS:
            amount = D(group[symbol])
            if amount <= 0:
                raise ValueError(f"va_plan.{group_name}.{symbol} 须大于零。")
            cleaned[symbol] = decimal_text(amount)
        va[group_name] = cleaned
    if not isinstance(va.get("sell_if_above_target"), bool):
        raise ValueError("va_plan.sell_if_above_target 须为 true 或 false。")
    va["start_month"] = va_start
    config["va_plan"] = va
    plan_start = config["monthly_plan_start"]
    if plan_start is not None:
        if not isinstance(plan_start, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", plan_start):
            raise ValueError("monthly_plan_start 须为 YYYY-MM 或 null。")
    initial = D(config["backtest_initial_usd"])
    if initial < 0:
        raise ValueError("backtest_initial_usd 不可为负。")
    backtest_monthly = D(config["backtest_monthly_budget_usd"])
    if backtest_monthly <= 0:
        raise ValueError("backtest_monthly_budget_usd 须大于零。")
    weights = config["target_weights"]
    if not isinstance(weights, dict) or set(weights) != set(SYMBOLS):
        raise ValueError("target_weights 须包含 VOO 和 QQQM。")
    a, b = D(weights["VOO"]), D(weights["QQQM"])
    if a < 0 or b < 0 or a + b != 100:
        raise ValueError("VOO/QQQM 目标比例须非负且合计 100。")
    if not isinstance(config["obsidian_sync_enabled"], bool):
        raise ValueError("obsidian_sync_enabled 须为 true 或 false。")
    subdir = Path(config["obsidian_subdir"])
    if subdir.is_absolute() or not subdir.parts or any(p in ("..", ".") for p in subdir.parts):
        raise ValueError("obsidian_subdir 须为 Vault 内的相对目录，不可使用 ..。")
    for key in ("database_path", "report_path", "backtest_output_dir",
                "va_backtest_output_dir", "opportunity_backtest_output_dir", "obsidian_vault_path"):
        if not isinstance(config[key], str) or not config[key].strip():
            raise ValueError(f"{key} 须为非空路径。")
    config["monthly_budget_usd"] = budget
    config["monthly_budget_schedule"] = schedule
    config["backtest_initial_usd"] = initial
    config["backtest_monthly_budget_usd"] = backtest_monthly
    config.pop("opportunity_backtest_cny_per_usd", None)
    config["target_weights"] = {"VOO": a, "QQQM": b}
    config["database_path"] = resolve_from_config(config_file, config["database_path"])
    config["report_path"] = resolve_from_config(config_file, config["report_path"])
    config["backtest_output_dir"] = resolve_from_config(config_file, config["backtest_output_dir"])
    config["va_backtest_output_dir"] = resolve_from_config(config_file, config["va_backtest_output_dir"])
    config["opportunity_backtest_output_dir"] = resolve_from_config(
        config_file, config["opportunity_backtest_output_dir"]
    )
    config["obsidian_vault_path"] = Path(config["obsidian_vault_path"]).expanduser().resolve()
    if config["database_path"].suffix.lower() not in (".sqlite3", ".sqlite", ".db"):
        raise ValueError("database_path 须以 .sqlite3、.sqlite 或 .db 结尾。")
    if config["report_path"].suffix.lower() != ".html":
        raise ValueError("report_path 须以 .html 结尾。")
    if len({config_file, config["database_path"], config["report_path"]}) != 3:
        raise ValueError("配置文件、数据库和报告路径不可相同。")
    return config


def atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def generated_note(path: Path, content: str, expected_id: int | None = None) -> None:
    """Only replace Markdown previously written by this tool."""
    if path.exists():
        with path.open(encoding="utf-8") as old_note:
            prefix = old_note.read(300)
        if (not prefix.startswith("---\ngenerator: voo_qqqm_investment_tool\n")
                or (expected_id is not None and f"transaction_id: {expected_id}\n" not in prefix)):
            raise ValueError(f"Obsidian 已有同名非本工具笔记，未覆盖：{path}")
    atomic_text(path, content)


META_BEGIN = "# VOO_QQQM_META_BEGIN"
META_END = "# VOO_QQQM_META_END"
BODY_BEGIN = "<!-- VOO_QQQM_BODY_BEGIN -->"
BODY_END = "<!-- VOO_QQQM_BODY_END -->"


def marked_overview(path: Path, metadata: str, body: str) -> None:
    """Change only two explicit blocks in the existing real-investment overview."""
    if path.exists():
        content = path.read_text(encoding="utf-8")
        if not all(content.count(marker) == 1 for marker in (META_BEGIN, META_END, BODY_BEGIN, BODY_END)):
            if any(marker in content for marker in (META_BEGIN, META_END, BODY_BEGIN, BODY_END)):
                raise ValueError(f"实盘总览标记区块不完整或重复，未覆盖：{path}")
            # Migrate only the recognizable full-page output of the previous version.
            if not content.startswith("---\ngenerator: voo_qqqm_investment_tool\ntype: investment_overview\n"):
                raise ValueError(f"实盘总览缺少唯一标记区块，未覆盖手写文档：{path}")
            old_meta = re.search(r"(?m)^as_of: .*\n(?:.*\n)*?^total_pnl_usd: .*\n", content)
            old_body = re.search(r"(?s)- 净入金：.*?> 本页由本地 SQLite 流水生成；参考估值不是实时行情。请在程序中修改原始记录。", content)
            if not old_meta or not old_body:
                raise ValueError(f"旧实盘总览格式无法安全迁移，未覆盖：{path}")
            content = content[:old_body.start()] + BODY_BEGIN + "\n" + content[old_body.start():old_body.end()] + "\n" + BODY_END + content[old_body.end():]
            content = content[:old_meta.start()] + META_BEGIN + "\n" + content[old_meta.start():old_meta.end()] + META_END + "\n" + content[old_meta.end():]
    else:
        content = ("---\ngenerator: voo_qqqm_investment_tool\ntype: investment_overview\n"
                   f"{META_BEGIN}\n{META_END}\ncurrency: USD\ntags: [投资, 美股]\n---\n\n"
                   f"# VOO / QQQM 实盘总览\n\n{BODY_BEGIN}\n{BODY_END}\n")
    for begin, end, replacement in ((META_BEGIN, META_END, metadata), (BODY_BEGIN, BODY_END, body)):
        if content.count(begin) != 1 or content.count(end) != 1 or content.index(begin) >= content.index(end):
            raise ValueError(f"实盘总览标记区块不完整，未覆盖：{path}")
        content = content[:content.index(begin) + len(begin)] + "\n" + replacement.rstrip() + "\n" + content[content.index(end):]
    atomic_text(path, content)


OPPORTUNITY_SCHEMA_VERSION = 4
VA_CALENDAR_SCHEMA_VERSION = 7
OPPORTUNITY_TABLES = {
    "opportunity_cny_events",
    "opportunity_stage_actions",
    "opportunity_action_transactions",
}


def _backup_before_opportunity_schema(con: sqlite3.Connection, db_path: Path) -> Path | None:
    """Create one timestamped snapshot before the first opportunity migration."""
    if con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0:
        return None
    backup_dir = db_path.parent / "backups"
    existing = sorted(backup_dir.glob(
        f"{db_path.stem}.pre-opportunity-v1-*{db_path.suffix}"
    )) if backup_dir.exists() else []
    if existing:
        return existing[-1]
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"{db_path.stem}.pre-opportunity-v1-{stamp}{db_path.suffix}"
    sequence = 1
    while backup_path.exists():
        backup_path = backup_dir / (
            f"{db_path.stem}.pre-opportunity-v1-{stamp}-{sequence}{db_path.suffix}"
        )
        sequence += 1
    with sqlite3.connect(backup_path) as backup:
        con.backup(backup)
    return backup_path


def _backup_before_usd_plan_schema(con: sqlite3.Connection, db_path: Path) -> Path | None:
    """Keep one auditable snapshot before adding USD opportunity columns."""
    if con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0:
        return None
    backup_dir = db_path.parent / "backups"
    existing = sorted(backup_dir.glob(
        f"{db_path.stem}.pre-opportunity-usd-*{db_path.suffix}"
    )) if backup_dir.exists() else []
    if existing:
        return existing[-1]
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"{db_path.stem}.pre-opportunity-usd-{stamp}{db_path.suffix}"
    with sqlite3.connect(backup_path) as backup:
        con.backup(backup)
    return backup_path


def _backup_before_va_calendar_schema(con: sqlite3.Connection, db_path: Path) -> Path | None:
    """Keep one verified snapshot before adding the VA calendar ledger."""
    if con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0:
        return None
    backup_dir = db_path.parent / "backups"
    existing = sorted(backup_dir.glob(
        f"{db_path.stem}.pre-va-calendar-*{db_path.suffix}"
    )) if backup_dir.exists() else []
    if existing:
        return existing[-1]
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"{db_path.stem}.pre-va-calendar-{stamp}{db_path.suffix}"
    temporary = backup_path.with_name(backup_path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        with sqlite3.connect(temporary) as backup:
            con.backup(backup)
            result = backup.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise ValueError(f"VA 日历迁移备份完整性检查失败：{result}")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(backup_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return backup_path


def _backup_before_va_budget_schema(con: sqlite3.Connection, db_path: Path) -> Path | None:
    """Keep one verified snapshot before adding the explicit VA budget date."""
    if con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0:
        return None
    backup_dir = db_path.parent / "backups"
    existing = sorted(backup_dir.glob(
        f"{db_path.stem}.pre-va-budget-day-*{db_path.suffix}"
    )) if backup_dir.exists() else []
    if existing:
        return existing[-1]
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"{db_path.stem}.pre-va-budget-day-{stamp}{db_path.suffix}"
    temporary = backup_path.with_name(backup_path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        with sqlite3.connect(temporary) as backup:
            con.backup(backup)
            result = backup.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise ValueError(f"VA 预算日迁移备份完整性检查失败：{result}")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(backup_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return backup_path


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        db_path.chmod(0o600)
    except OSError:
        pass
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('deposit','withdrawal','buy','sell','dividend')),
            symbol TEXT CHECK(symbol IN ('VOO','QQQM') OR symbol IS NULL),
            amount TEXT NOT NULL,
            price TEXT,
            shares TEXT,
            fee TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_transactions_date ON transactions(date, id);
        CREATE TABLE IF NOT EXISTS quotes (
            symbol TEXT PRIMARY KEY CHECK(symbol IN ('VOO','QQQM')),
            as_of TEXT NOT NULL,
            price TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual' CHECK(source IN ('manual','market'))
        );
        CREATE TABLE IF NOT EXISTS quote_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL CHECK(symbol IN ('VOO','QQQM')),
            as_of TEXT NOT NULL,
            price TEXT NOT NULL,
            source TEXT NOT NULL CHECK(source IN ('manual','market')),
            replaced_as_of TEXT,
            replaced_price TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS rule_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            symbol TEXT NOT NULL CHECK(symbol IN ('VOO','QQQM')),
            observation_json TEXT NOT NULL,
            evaluation_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS transaction_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id INTEGER,
            action TEXT NOT NULL CHECK(action IN ('create','update','delete','batch_create')),
            before_json TEXT,
            after_json TEXT,
            reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS ix_transaction_revisions_transaction
            ON transaction_revisions(transaction_id, id);
    """)
    existing = {r[1] for r in con.execute("PRAGMA table_info(transactions)")}
    additions = {
        "source_type": "TEXT CHECK(source_type IN ('fixed','extra') OR source_type IS NULL)",
        "plan_month": "TEXT",
        "trigger_note": "TEXT NOT NULL DEFAULT ''",
        "transfer_date": "TEXT",
        "hkd_sent": "TEXT",
        "fx_rate_hkd_per_usd": "TEXT",
        "fx_cost_hkd": "TEXT",
    }
    missing = [column for column in additions if column not in existing]
    if missing and con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]:
        backup_path = db_path.with_name(db_path.stem + ".pre-v2" + db_path.suffix)
        if not backup_path.exists():
            with sqlite3.connect(backup_path) as backup:
                con.backup(backup)
    for column, definition in additions.items():
        if column not in existing:
            con.execute(f"ALTER TABLE transactions ADD COLUMN {column} {definition}")
    quote_columns = {r[1] for r in con.execute("PRAGMA table_info(quotes)")}
    if "source" not in quote_columns:
        con.execute("ALTER TABLE quotes ADD COLUMN source TEXT NOT NULL DEFAULT 'manual' "
                    "CHECK(source IN ('manual','market'))")
    con.commit()
    existing_tables = {
        row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?)",
            tuple(sorted(OPPORTUNITY_TABLES)),
        )
    }
    if existing_tables != OPPORTUNITY_TABLES:
        _backup_before_opportunity_schema(con, db_path)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS opportunity_cny_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_date TEXT NOT NULL,
            plan_month TEXT,
            event_type TEXT NOT NULL CHECK(event_type IN (
                'monthly_reserve','convert_to_usd','excess_to_fixed',
                'adjustment_in','adjustment_out'
            )),
            status TEXT NOT NULL DEFAULT 'completed'
                CHECK(status IN ('planned','completed','cancelled')),
            amount_cny TEXT NOT NULL CHECK(CAST(amount_cny AS REAL) > 0),
            completed_on TEXT,
            linked_transaction_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK(plan_month IS NULL OR (
                length(plan_month) = 7 AND substr(plan_month,5,1) = '-'
            )),
            CHECK(status != 'completed' OR completed_on IS NOT NULL)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_opportunity_cny_monthly_reserve
            ON opportunity_cny_events(plan_month)
            WHERE event_type='monthly_reserve' AND status!='cancelled';
        CREATE INDEX IF NOT EXISTS ix_opportunity_cny_date
            ON opportunity_cny_events(event_date, id);
        CREATE INDEX IF NOT EXISTS ix_opportunity_cny_status_month
            ON opportunity_cny_events(status, plan_month);

        CREATE TABLE IF NOT EXISTS opportunity_stage_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_date TEXT NOT NULL,
            market_date TEXT NOT NULL,
            cycle_key TEXT NOT NULL,
            rule_hash TEXT NOT NULL,
            trigger_symbol TEXT NOT NULL CHECK(trigger_symbol IN ('VOO','QQQM')),
            stage INTEGER NOT NULL CHECK(stage BETWEEN 1 AND 4),
            reference_high_date TEXT NOT NULL,
            reference_high_close TEXT NOT NULL CHECK(CAST(reference_high_close AS REAL) > 0),
            drawdown_pct TEXT NOT NULL CHECK(
                CAST(drawdown_pct AS REAL) >= 0 AND CAST(drawdown_pct AS REAL) <= 100
            ),
            rule_ratio_pct TEXT NOT NULL CHECK(
                CAST(rule_ratio_pct AS REAL) > 0 AND CAST(rule_ratio_pct AS REAL) <= 100
            ),
            pool_cny_before TEXT NOT NULL CHECK(CAST(pool_cny_before AS REAL) >= 0),
            rule_amount_cny TEXT NOT NULL CHECK(CAST(rule_amount_cny AS REAL) >= 0),
            effective_amount_cny TEXT NOT NULL CHECK(CAST(effective_amount_cny AS REAL) >= 0),
            pool_usd_before TEXT NOT NULL CHECK(CAST(pool_usd_before AS REAL) >= 0),
            risk_status TEXT NOT NULL DEFAULT 'pending'
                CHECK(risk_status IN ('pending','full','half','pause')),
            approved_amount_usd TEXT CHECK(
                approved_amount_usd IS NULL OR CAST(approved_amount_usd AS REAL) >= 0
            ),
            manual_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(manual_confirmed IN (0,1)),
            status TEXT NOT NULL DEFAULT 'draft'
                CHECK(status IN ('draft','approved','executed','cancelled')),
            review_json TEXT NOT NULL DEFAULT '{}',
            note TEXT NOT NULL DEFAULT '',
            executed_on TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK(status != 'executed' OR executed_on IS NOT NULL)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_opportunity_executed_stage
            ON opportunity_stage_actions(rule_hash, cycle_key, stage)
            WHERE status='executed';
        CREATE INDEX IF NOT EXISTS ix_opportunity_action_cycle
            ON opportunity_stage_actions(rule_hash, cycle_key, stage, status);
        CREATE INDEX IF NOT EXISTS ix_opportunity_action_market_date
            ON opportunity_stage_actions(market_date, id);

        CREATE TABLE IF NOT EXISTS opportunity_action_transactions (
            action_id INTEGER NOT NULL
                REFERENCES opportunity_stage_actions(id) ON DELETE RESTRICT,
            transaction_id INTEGER NOT NULL
                REFERENCES transactions(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(action_id, transaction_id),
            UNIQUE(transaction_id)
        );
        CREATE INDEX IF NOT EXISTS ix_opportunity_action_transaction
            ON opportunity_action_transactions(transaction_id);
    """)
    current_version = con.execute("PRAGMA user_version").fetchone()[0]
    action_columns = {
        row[1] for row in con.execute("PRAGMA table_info(opportunity_stage_actions)")
    }
    usd_action_columns = {
        "rule_amount_usd": "TEXT NOT NULL DEFAULT '0' CHECK(CAST(rule_amount_usd AS REAL) >= 0)",
        "effective_amount_usd": "TEXT NOT NULL DEFAULT '0' CHECK(CAST(effective_amount_usd AS REAL) >= 0)",
    }
    missing_usd_columns = [key for key in usd_action_columns if key not in action_columns]
    if missing_usd_columns:
        _backup_before_usd_plan_schema(con, db_path)
    for column, definition in usd_action_columns.items():
        if column not in action_columns:
            con.execute(
                f"ALTER TABLE opportunity_stage_actions ADD COLUMN {column} {definition}"
            )
    if current_version < OPPORTUNITY_SCHEMA_VERSION:
        con.execute(f"PRAGMA user_version = {OPPORTUNITY_SCHEMA_VERSION}")
    con.commit()
    calendar_table_exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='va_month_checkpoints'"
    ).fetchone() is not None
    if not calendar_table_exists:
        _backup_before_va_calendar_schema(con, db_path)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS va_month_checkpoints (
            plan_month TEXT NOT NULL,
            symbol TEXT NOT NULL CHECK(symbol IN ('VOO','QQQM')),
            target_value_usd TEXT NOT NULL CHECK(CAST(target_value_usd AS REAL) >= 0),
            required_buy_usd TEXT NOT NULL CHECK(CAST(required_buy_usd AS REAL) >= 0),
            reference_market_value_usd TEXT NOT NULL
                CHECK(CAST(reference_market_value_usd AS REAL) >= 0),
            reference_shares TEXT NOT NULL DEFAULT '0'
                CHECK(CAST(reference_shares AS REAL) >= 0),
            reference_price_usd TEXT
                CHECK(reference_price_usd IS NULL OR CAST(reference_price_usd AS REAL) > 0),
            reference_price_date TEXT,
            reference_price_source TEXT NOT NULL DEFAULT '',
            budget_date TEXT,
            evaluated_on TEXT NOT NULL,
            freeze_reason TEXT NOT NULL DEFAULT 'manual'
                CHECK(freeze_reason IN ('manual','first_buy','existing_buys')),
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(plan_month, symbol),
            CHECK(length(plan_month)=7 AND substr(plan_month,5,1)='-')
        );
        CREATE INDEX IF NOT EXISTS ix_va_month_checkpoints_evaluated
            ON va_month_checkpoints(evaluated_on, plan_month, symbol);
    """)
    checkpoint_columns = {
        row[1] for row in con.execute("PRAGMA table_info(va_month_checkpoints)")
    }
    if "budget_date" not in checkpoint_columns:
        _backup_before_va_budget_schema(con, db_path)
        con.execute("ALTER TABLE va_month_checkpoints ADD COLUMN budget_date TEXT")
    # Version 5 checkpoints did not store the decision date.  This personal
    # ledger adopts day 2 as the new real-account rule, so persist that date
    # during migration instead of deriving historical rows from a future config
    # change.  New checkpoints always write their configured date explicitly.
    legacy_budget_day = int(DEFAULTS["budget_day"])
    missing_budget_date = con.execute(
        "SELECT 1 FROM va_month_checkpoints "
        "WHERE budget_date IS NULL OR trim(budget_date) = '' LIMIT 1"
    ).fetchone()
    if missing_budget_date is not None:
        con.execute(
            "UPDATE va_month_checkpoints SET budget_date = plan_month || ? "
            "WHERE budget_date IS NULL OR trim(budget_date) = ''",
            (f"-{legacy_budget_day:02d}",),
        )
    if con.execute("PRAGMA user_version").fetchone()[0] < VA_CALENDAR_SCHEMA_VERSION:
        con.execute(f"PRAGMA user_version = {VA_CALENDAR_SCHEMA_VERSION}")
    con.commit()
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='stock_splits'").fetchone() is None:
        _daily_backup_before_mutation(con)
        con.executescript("""
            CREATE TABLE stock_splits (
                id INTEGER PRIMARY KEY, symbol TEXT NOT NULL CHECK(symbol IN ('VOO','QQQM')),
                date TEXT NOT NULL, ratio TEXT NOT NULL CHECK(CAST(ratio AS REAL)>0),
                note TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(symbol,date)
            );
        """)
    con.commit()
    return con


class LedgerRows(list):
    def __init__(self, records, splits=()):
        super().__init__(records)
        self.splits = list(splits)


def rows(con: sqlite3.Connection) -> list[sqlite3.Row]:
    records=con.execute("SELECT * FROM transactions ORDER BY date, id").fetchall()
    splits=con.execute("SELECT * FROM stock_splits ORDER BY date,id").fetchall()
    return LedgerRows(records,splits)


def pending_stock_splits(con: sqlite3.Connection, config: dict) -> list[dict]:
    from market_prices import actions_for
    events=actions_for(Path(config["database_path"]).parent)
    existing={(r["symbol"],r["date"]) for r in con.execute("SELECT * FROM stock_splits")}
    records=rows(con);pending=[]
    for event in events:
        if event['type']!='split' or (event['symbol'],event['date']) in existing:continue
        earlier=LedgerRows([r for r in records if r['date']<event['date']],
                           [r for r in records.splits if r['date']<event['date']])
        if calculate(earlier,{})['positions'][event['symbol']].shares>0:
            pending.append(event)
    return pending


def record_stock_split(con: sqlite3.Connection, config: dict, symbol: str, day: str,
                       ratio: object, note: str) -> None:
    day=parse_day(day);ratio=D(ratio)
    if not note.strip():raise ValueError("请填写 Schwab 拆股核对备注。")
    event=next((e for e in pending_stock_splits(con,config)
                if e['symbol']==symbol and e['date']==day and D(e['ratio'])==ratio),None)
    if event is None:raise ValueError("拆股事件已处理或与下载资料不一致，请刷新页面重新核对。")
    _daily_backup_before_mutation(con)
    with con:
        con.execute("INSERT INTO stock_splits(symbol,date,ratio,note) VALUES (?,?,?,?)",
                    (symbol,day,decimal_text(ratio),note.strip()))
        records=rows(con);calculate(records,get_quotes(con));validate_cash_history(records)


def _daily_backup_before_mutation(con: sqlite3.Connection, *, keep: int = 30) -> Path | None:
    """Create at most one verified SQLite snapshot per local calendar day."""
    database_name = next(
        (str(row[2]) for row in con.execute("PRAGMA database_list") if row[1] == "main"),
        "",
    )
    if not database_name:
        return None
    database_file = Path(database_name)
    backup_dir = database_file.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    try:
        backup_dir.chmod(0o700)
    except OSError:
        pass
    backup_path = backup_dir / (
        f"{database_file.stem}.daily-{date.today().strftime('%Y%m%d')}{database_file.suffix}"
    )
    if not backup_path.exists():
        temporary = backup_path.with_name(backup_path.name + ".tmp")
        temporary.unlink(missing_ok=True)
        try:
            with sqlite3.connect(temporary) as backup:
                con.backup(backup)
                result = backup.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    raise ValueError(f"账本日备份完整性检查失败：{result}")
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            temporary.replace(backup_path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    snapshots = sorted(backup_dir.glob(f"{database_file.stem}.daily-*{database_file.suffix}"))
    for obsolete in snapshots[:-max(1, keep)]:
        obsolete.unlink(missing_ok=True)
    return backup_path


def _revision_json(row: sqlite3.Row | None) -> str | None:
    return json.dumps(dict(row), ensure_ascii=False, sort_keys=True) if row is not None else None


def _record_revision(con: sqlite3.Connection, transaction_id: int, action: str,
                     before: sqlite3.Row | None, after: sqlite3.Row | None,
                     reason: str = "") -> None:
    con.execute(
        "INSERT INTO transaction_revisions(transaction_id,action,before_json,after_json,reason) "
        "VALUES (?,?,?,?,?)",
        (int(transaction_id), action, _revision_json(before), _revision_json(after), reason),
    )


OPPORTUNITY_CNY_INFLOW_TYPES = {"monthly_reserve", "adjustment_in"}
OPPORTUNITY_CNY_OUTFLOW_TYPES = {"convert_to_usd", "excess_to_fixed", "adjustment_out"}
OPPORTUNITY_CNY_EVENT_TYPES = OPPORTUNITY_CNY_INFLOW_TYPES | OPPORTUNITY_CNY_OUTFLOW_TYPES
OPPORTUNITY_CNY_STATUSES = {"planned", "completed", "cancelled"}
OPPORTUNITY_RISK_STATUSES = {"pending", "full", "half", "pause"}
OPPORTUNITY_ACTION_STATUSES = {"draft", "approved", "executed", "cancelled"}


def _calendar_day(value: object, *, allow_future: bool = False) -> str:
    raw = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raise ValueError("日期须为 YYYY-MM-DD。")
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("日期须为有效的 YYYY-MM-DD。") from exc
    if not allow_future and parsed > date.today():
        raise ValueError("日期不可晚于今天。")
    return parsed.isoformat()


def _plan_month(value: object, *, required: bool = False) -> str | None:
    raw = str(value or "").strip()
    if not raw and not required:
        return None
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", raw):
        raise ValueError("计划月份须为 YYYY-MM。")
    return raw


def list_opportunity_cny_events(con: sqlite3.Connection, *, include_cancelled: bool = True) -> list[dict]:
    where = "" if include_cancelled else "WHERE status!='cancelled'"
    return [dict(row) for row in con.execute(
        f"SELECT * FROM opportunity_cny_events {where} ORDER BY event_date, id"
    )]


def _validate_opportunity_cny_event(con: sqlite3.Connection, event: dict) -> dict:
    if not isinstance(event, dict):
        raise ValueError("机会资金事件格式不正确。")
    item = dict(event)
    event_type = str(item.get("event_type") or "")
    status = str(item.get("status") or "completed")
    if event_type not in OPPORTUNITY_CNY_EVENT_TYPES:
        raise ValueError("机会资金事件类型不正确。")
    if status not in OPPORTUNITY_CNY_STATUSES:
        raise ValueError("机会资金事件状态不正确。")
    if event_type == "monthly_reserve" and status != "completed":
        raise ValueError("月度预留只记录已经实际预留的人民币。")
    amount = D(item.get("amount_cny"))
    if amount <= 0:
        raise ValueError("人民币金额须大于零。")
    needs_month = event_type in ("monthly_reserve", "excess_to_fixed")
    plan_month = _plan_month(item.get("plan_month"), required=needs_month)
    event_day = _calendar_day(item.get("event_date"), allow_future=status == "planned")
    completed_on = item.get("completed_on") or None
    if status == "completed":
        completed_on = _calendar_day(completed_on or event_day)
        if event_day > completed_on and event_type != "excess_to_fixed":
            raise ValueError("完成日期不可早于事件日期。")
    elif completed_on:
        raise ValueError("只有已完成事件可填写完成日期。")
    linked_transaction_id = item.get("linked_transaction_id")
    if linked_transaction_id in (None, ""):
        linked_transaction_id = None
    else:
        linked_transaction_id = int(linked_transaction_id)
        transaction = con.execute(
            "SELECT id,kind,source_type FROM transactions WHERE id=?",
            (linked_transaction_id,),
        ).fetchone()
        if transaction is None:
            raise ValueError("关联的真实流水不存在。")
        expected_source = "extra" if event_type == "convert_to_usd" else (
            "fixed" if event_type == "excess_to_fixed" else None
        )
        if expected_source is None:
            raise ValueError("该类人民币事件不应关联美元流水。")
        if transaction["kind"] != "deposit" or transaction["source_type"] != expected_source:
            label = "额外资金入金" if expected_source == "extra" else "固定定投入金"
            raise ValueError(f"该事件只能关联一笔{label}流水。")
    return {
        "event_date": event_day,
        "plan_month": plan_month,
        "event_type": event_type,
        "status": status,
        "amount_cny": decimal_text(amount),
        "completed_on": completed_on,
        "linked_transaction_id": linked_transaction_id,
        "note": str(item.get("note") or ""),
    }


def _validate_opportunity_cny_history(con: sqlite3.Connection) -> None:
    balance = Decimal(0)
    completed = con.execute(
        "SELECT * FROM opportunity_cny_events WHERE status='completed' "
        "ORDER BY completed_on, id"
    ).fetchall()
    for row in completed:
        amount = D(row["amount_cny"])
        balance += amount if row["event_type"] in OPPORTUNITY_CNY_INFLOW_TYPES else -amount
        if balance < 0:
            raise ValueError(
                f"{row['completed_on']} 人民币机会资金余额不足；请检查事件 #{row['id']}。"
            )


def save_opportunity_cny_event(con: sqlite3.Connection, event: dict,
                               event_id: int | None = None) -> int:
    """Insert or update one actual/planned CNY event without touching USD transactions."""
    item = _validate_opportunity_cny_event(con, event)
    fields = (
        item["event_date"], item["plan_month"], item["event_type"], item["status"],
        item["amount_cny"], item["completed_on"], item["linked_transaction_id"],
        item["note"],
    )
    try:
        with con:
            if event_id is None:
                cursor = con.execute(
                    "INSERT INTO opportunity_cny_events("
                    "event_date,plan_month,event_type,status,amount_cny,completed_on,"
                    "linked_transaction_id,note) VALUES (?,?,?,?,?,?,?,?)",
                    fields,
                )
                event_id = int(cursor.lastrowid)
            else:
                event_id = int(event_id)
                cursor = con.execute(
                    "UPDATE opportunity_cny_events SET event_date=?,plan_month=?,event_type=?,"
                    "status=?,amount_cny=?,completed_on=?,linked_transaction_id=?,note=?,"
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    fields + (event_id,),
                )
                if cursor.rowcount == 0:
                    raise ValueError("人民币机会资金事件不存在。")
            _validate_opportunity_cny_history(con)
    except sqlite3.IntegrityError as exc:
        if item["event_type"] == "monthly_reserve":
            raise ValueError(f"{item['plan_month']} 已记录月度机会资金预留。") from exc
        raise ValueError("人民币机会资金事件违反数据库约束。") from exc
    return event_id


def set_opportunity_cny_event_status(con: sqlite3.Connection, event_id: int, status: str,
                                     *, completed_on: str | None = None) -> int:
    row = con.execute("SELECT * FROM opportunity_cny_events WHERE id=?", (int(event_id),)).fetchone()
    if row is None:
        raise ValueError("人民币机会资金事件不存在。")
    item = dict(row)
    item["status"] = status
    item["completed_on"] = completed_on
    return save_opportunity_cny_event(con, item, int(event_id))


def delete_opportunity_cny_event(con: sqlite3.Connection, event_id: int) -> None:
    with con:
        cursor = con.execute("DELETE FROM opportunity_cny_events WHERE id=?", (int(event_id),))
        if cursor.rowcount == 0:
            raise ValueError("人民币机会资金事件不存在。")
        _validate_opportunity_cny_history(con)


def opportunity_cny_summary(con: sqlite3.Connection, config: dict,
                            today: date | None = None) -> dict:
    """Return actual CNY reserve metrics; never converts planned CNY into tradable USD."""
    today = today or date.today()
    current_month = today.strftime("%Y-%m")
    monthly_target = D(config["opportunity_plan"]["monthly_cny"])
    cap = D(config["opportunity_plan"]["cap_cny"])
    balance = monthly_actual = total_reserved = total_converted = Decimal(0)
    total_excess_completed = planned_excess = Decimal(0)
    for row in con.execute("SELECT * FROM opportunity_cny_events ORDER BY event_date,id"):
        amount = D(row["amount_cny"])
        if row["status"] == "planned" and row["event_type"] == "excess_to_fixed":
            planned_excess += amount
        if row["status"] != "completed":
            continue
        if row["event_type"] in OPPORTUNITY_CNY_INFLOW_TYPES:
            balance += amount
        else:
            balance -= amount
        if row["event_type"] == "monthly_reserve":
            total_reserved += amount
            if row["plan_month"] == current_month:
                monthly_actual += amount
        elif row["event_type"] == "convert_to_usd":
            total_converted += amount
        elif row["event_type"] == "excess_to_fixed":
            total_excess_completed += amount
    excess = max(Decimal(0), balance - cap)
    monthly_remaining = max(Decimal(0), monthly_target - monthly_actual)
    monthly_overage = max(Decimal(0), monthly_actual - monthly_target)
    completion = monthly_actual / monthly_target * 100 if monthly_target else Decimal(0)
    executed_count = con.execute(
        "SELECT COUNT(*) FROM opportunity_stage_actions WHERE status='executed'"
    ).fetchone()[0]
    return {
        "current_month": current_month,
        "monthly_target_cny": monthly_target,
        "monthly_actual_cny": monthly_actual,
        "monthly_remaining_cny": monthly_remaining,
        "monthly_overage_cny": monthly_overage,
        "monthly_completion_pct": min(Decimal(100), completion),
        "balance_cny": balance,
        "cap_cny": cap,
        "excess_cny": excess,
        "planned_excess_cny": planned_excess,
        "unplanned_excess_cny": max(Decimal(0), excess - planned_excess),
        "total_reserved_cny": total_reserved,
        "total_converted_to_usd_cny": total_converted,
        "total_excess_to_fixed_cny": total_excess_completed,
        "excess_spread_months": config["opportunity_plan"]["excess_spread_months"],
        "executed_stage_count": int(executed_count),
    }


def _review_json(value: object) -> tuple[dict, str]:
    if value in (None, ""):
        parsed = {}
    elif isinstance(value, dict):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("风险复核内容不是有效 JSON。") from exc
    else:
        raise ValueError("风险复核内容须为 JSON 对象。")
    if not isinstance(parsed, dict):
        raise ValueError("风险复核内容须为 JSON 对象。")
    return parsed, json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


def _validate_opportunity_stage_action(action: dict, config: dict) -> dict:
    if not isinstance(action, dict):
        raise ValueError("机会档位记录格式不正确。")
    item = dict(action)
    stage = int(item.get("stage"))
    if stage not in (1, 2, 3, 4):
        raise ValueError("机会档位须为 1–4。")
    decision_day = _calendar_day(item.get("decision_date"))
    market_day = _calendar_day(item.get("market_date"))
    high_day = _calendar_day(item.get("reference_high_date"))
    if not high_day <= market_day <= decision_day:
        raise ValueError("参考高点、市场数据与决策日期的先后顺序不正确。")
    cycle_key = str(item.get("cycle_key") or "").strip()
    if not cycle_key or len(cycle_key) > 200:
        raise ValueError("机会周期标识不可为空且不得超过 200 个字符。")
    rule_hash = str(item.get("rule_hash") or "").strip()
    if not rule_hash or len(rule_hash) > 200:
        raise ValueError("机会规则版本标识不可为空且不得超过 200 个字符。")
    trigger_symbol = str(item.get("trigger_symbol") or "").upper()
    if trigger_symbol not in SYMBOLS:
        raise ValueError("触发标的须为 VOO 或 QQQM。")
    reference_high = D(item.get("reference_high_close"))
    drawdown = D(item.get("drawdown_pct"))
    pool_usd_before = D(item.get("pool_usd_before"))
    if (reference_high <= 0 or drawdown < 0 or drawdown > 100 or
            pool_usd_before < 0):
        raise ValueError("高点、回撤或机会资金池数值不正确。")
    configured_ratio = D(config["opportunity_plan"]["stage_ratios_pct"][stage - 1])
    ratio = D(item.get("rule_ratio_pct", configured_ratio))
    if ratio != configured_ratio:
        raise ValueError(f"第 {stage} 档使用比例须为配置中的 {configured_ratio}%。")
    rule_base_usd = min(pool_usd_before, D(config["opportunity_plan"]["cap_usd"]))
    rule_amount_usd = (rule_base_usd * ratio / 100).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    supplied_amount = item.get("rule_amount_usd")
    if (supplied_amount not in (None, "") and
            abs(D(supplied_amount) - rule_amount_usd) > Decimal("0.01")):
        raise ValueError("档位规划金额须按决策前的可部署美元机会资金计算。")
    risk_status = str(item.get("risk_status") or "pending")
    # Accept the early internal name while storing the user-facing V1 term.
    if risk_status == "pass":
        risk_status = "full"
    status = str(item.get("status") or "draft")
    if risk_status not in OPPORTUNITY_RISK_STATUSES:
        raise ValueError("风险复核状态不正确。")
    if status not in OPPORTUNITY_ACTION_STATUSES or status == "executed":
        raise ValueError("保存档位时仅可使用草稿、已批准或已取消状态；执行须走单独操作。")
    manual_confirmed = item.get("manual_confirmed") is True or item.get("manual_confirmed") == 1
    review, review_text = _review_json(
        item.get("review_json") if "review_json" in item else item.get("review")
    )
    if risk_status == "full":
        effective_amount_usd = rule_amount_usd
    elif risk_status == "half":
        effective_amount_usd = (rule_amount_usd * D(
            config["opportunity_plan"]["risk_half_multiplier_pct"]
        ) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    else:
        effective_amount_usd = Decimal(0)
    supplied_effective = item.get("effective_amount_usd")
    if (supplied_effective not in (None, "") and
            abs(D(supplied_effective) - effective_amount_usd) > Decimal("0.01")):
        raise ValueError("风险调整后美元金额与 full/half/pause 复核结果不一致。")
    approved_raw = item.get("approved_amount_usd")
    approved = D(approved_raw) if approved_raw not in (None, "") else None
    if approved is not None and (
            approved <= 0 or approved > pool_usd_before or approved > effective_amount_usd):
        raise ValueError(
            "批准的实际美元上限须大于 0，且不得超过风险调整后的本档金额"
            f" {effective_amount_usd:.2f} USD 或实际可用额外资金 {pool_usd_before:.2f} USD。"
        )
    if status == "approved":
        if risk_status not in ("full", "half"):
            raise ValueError("风险复核为全额或减半后才能批准本档。")
        if effective_amount_usd <= 0:
            raise ValueError("美元机会资金不足，当前档位没有可批准的计划金额。")
        if config["opportunity_plan"]["require_risk_review"] and (not manual_confirmed or not review):
            raise ValueError("批准本档前须完成风险复核并人工确认。")
        if approved is None:
            raise ValueError("批准本档时须手工填写实际可用美元上限。")
    return {
        "decision_date": decision_day,
        "market_date": market_day,
        "cycle_key": cycle_key,
        "rule_hash": rule_hash,
        "trigger_symbol": trigger_symbol,
        "stage": stage,
        "reference_high_date": high_day,
        "reference_high_close": decimal_text(reference_high),
        "drawdown_pct": decimal_text(drawdown),
        "rule_ratio_pct": decimal_text(ratio),
        # Legacy CNY columns remain zero so older databases can be migrated in
        # place without changing any historical real transaction rows.
        "pool_cny_before": "0",
        "rule_amount_cny": "0",
        "effective_amount_cny": "0",
        "pool_usd_before": decimal_text(pool_usd_before),
        "rule_amount_usd": decimal_text(rule_amount_usd),
        "effective_amount_usd": decimal_text(effective_amount_usd),
        "risk_status": risk_status,
        "approved_amount_usd": decimal_text(approved) if approved is not None else None,
        "manual_confirmed": 1 if manual_confirmed else 0,
        "status": status,
        "review_json": review_text,
        "note": str(item.get("note") or ""),
    }


def save_opportunity_stage_action(con: sqlite3.Connection, action: dict, config: dict,
                                  action_id: int | None = None) -> int:
    """Save a reviewable stage candidate.  Only execute_* can consume a stage."""
    item = _validate_opportunity_stage_action(action, config)
    fields = tuple(item[key] for key in (
        "decision_date", "market_date", "cycle_key", "rule_hash", "trigger_symbol", "stage",
        "reference_high_date", "reference_high_close", "drawdown_pct", "rule_ratio_pct",
        "pool_cny_before", "rule_amount_cny", "effective_amount_cny", "pool_usd_before",
        "rule_amount_usd", "effective_amount_usd",
        "risk_status", "approved_amount_usd", "manual_confirmed", "status", "review_json", "note",
    ))
    with con:
        executed = con.execute(
            "SELECT id FROM opportunity_stage_actions "
            "WHERE rule_hash=? AND cycle_key=? AND stage=? AND status='executed'",
            (item["rule_hash"], item["cycle_key"], item["stage"]),
        ).fetchone()
        if executed is not None and (action_id is None or int(action_id) != executed["id"]):
            raise ValueError("本轮该档位已经执行，不可再次批准。")
        if action_id is None:
            cursor = con.execute(
                "INSERT INTO opportunity_stage_actions("
                "decision_date,market_date,cycle_key,rule_hash,trigger_symbol,stage,"
                "reference_high_date,reference_high_close,drawdown_pct,rule_ratio_pct,"
                "pool_cny_before,rule_amount_cny,effective_amount_cny,pool_usd_before,"
                "rule_amount_usd,effective_amount_usd,risk_status,approved_amount_usd,"
                "manual_confirmed,status,review_json,note) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                fields,
            )
            return int(cursor.lastrowid)
        action_id = int(action_id)
        old = con.execute(
            "SELECT status FROM opportunity_stage_actions WHERE id=?", (action_id,)
        ).fetchone()
        if old is None:
            raise ValueError("机会档位记录不存在。")
        if old["status"] == "executed":
            raise ValueError("已执行档位不可修改。")
        if item["status"] == "cancelled" and con.execute(
            "SELECT 1 FROM opportunity_action_transactions WHERE action_id=?", (action_id,)
        ).fetchone():
            raise ValueError("该档位已关联真实买入；请先解除关联再取消。")
        con.execute(
            "UPDATE opportunity_stage_actions SET decision_date=?,market_date=?,cycle_key=?,"
            "rule_hash=?,trigger_symbol=?,stage=?,reference_high_date=?,reference_high_close=?,"
            "drawdown_pct=?,rule_ratio_pct=?,pool_cny_before=?,rule_amount_cny=?,"
            "effective_amount_cny=?,pool_usd_before=?,rule_amount_usd=?,"
            "effective_amount_usd=?,risk_status=?,approved_amount_usd=?,"
            "manual_confirmed=?,status=?,review_json=?,note=?,"
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            fields + (action_id,),
        )
        return action_id


def list_opportunity_stage_actions(con: sqlite3.Connection, *, cycle_key: str | None = None,
                                   rule_hash: str | None = None,
                                   include_cancelled: bool = True) -> list[dict]:
    clauses, params = [], []
    if cycle_key is not None:
        clauses.append("a.cycle_key=?")
        params.append(cycle_key)
    if rule_hash is not None:
        clauses.append("a.rule_hash=?")
        params.append(rule_hash)
    if not include_cancelled:
        clauses.append("a.status!='cancelled'")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    result = []
    for row in con.execute(
        "SELECT a.* FROM opportunity_stage_actions a" + where +
        " ORDER BY a.market_date DESC,a.id DESC", params
    ):
        item = dict(row)
        for legacy_key in ("pool_cny_before", "rule_amount_cny", "effective_amount_cny"):
            item.pop(legacy_key, None)
        item["manual_confirmed"] = bool(item["manual_confirmed"])
        item["review"] = json.loads(item.pop("review_json"))
        linked_rows = con.execute(
            "SELECT t.id,t.symbol,t.amount,t.fee FROM transactions t "
            "JOIN opportunity_action_transactions l ON l.transaction_id=t.id "
            "WHERE l.action_id=? ORDER BY t.id", (item["id"],)
        ).fetchall()
        item["transaction_ids"] = [linked["id"] for linked in linked_rows]
        linked_amounts = {symbol: Decimal(0) for symbol in SYMBOLS}
        linked_fees = Decimal(0)
        for linked in linked_rows:
            linked_amounts[linked["symbol"]] += D(linked["amount"])
            linked_fees += D(linked["fee"])
        item["linked_amounts_usd"] = {
            symbol: decimal_text(amount) for symbol, amount in linked_amounts.items()
        }
        item["linked_fees_usd"] = decimal_text(linked_fees)
        item["linked_total_usd"] = decimal_text(sum(linked_amounts.values(), linked_fees))
        result.append(item)
    return result


def link_opportunity_transaction(con: sqlite3.Connection, action_id: int,
                                 transaction_id: int) -> None:
    action_id, transaction_id = int(action_id), int(transaction_id)
    with con:
        action = con.execute(
            "SELECT * FROM opportunity_stage_actions WHERE id=?", (action_id,)
        ).fetchone()
        if action is None:
            raise ValueError("机会档位记录不存在。")
        if action["status"] != "approved":
            raise ValueError("只有已批准且尚未执行的档位可关联真实买入。")
        transaction = con.execute(
            "SELECT * FROM transactions WHERE id=?", (transaction_id,)
        ).fetchone()
        if transaction is None:
            raise ValueError("真实流水不存在。")
        if transaction["kind"] != "buy" or transaction["source_type"] != "extra":
            raise ValueError("机会档位只能关联额外资金的真实买入流水。")
        if transaction["date"] < action["market_date"]:
            raise ValueError("关联买入日期不可早于本档使用的市场数据日期。")
        try:
            con.execute(
                "INSERT INTO opportunity_action_transactions(action_id,transaction_id) VALUES (?,?)",
                (action_id, transaction_id),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("该真实买入已经关联机会档位。") from exc
        linked_total = sum((D(row["amount"]) + D(row["fee"]) for row in con.execute(
            "SELECT t.amount,t.fee FROM transactions t "
            "JOIN opportunity_action_transactions l ON l.transaction_id=t.id "
            "WHERE l.action_id=?", (action_id,)
        )), Decimal(0))
        if linked_total > D(action["approved_amount_usd"]) + Decimal("0.02"):
            raise ValueError("关联买入的金额与费用合计超过本档批准上限。")


def unlink_opportunity_transaction(con: sqlite3.Connection, action_id: int,
                                   transaction_id: int) -> None:
    with con:
        action = con.execute(
            "SELECT status FROM opportunity_stage_actions WHERE id=?", (int(action_id),)
        ).fetchone()
        if action is None:
            raise ValueError("机会档位记录不存在。")
        if action["status"] == "executed":
            raise ValueError("已执行档位不可解除真实流水关联。")
        cursor = con.execute(
            "DELETE FROM opportunity_action_transactions WHERE action_id=? AND transaction_id=?",
            (int(action_id), int(transaction_id)),
        )
        if cursor.rowcount == 0:
            raise ValueError("未找到该档位与流水的关联。")


def execute_opportunity_stage_action(con: sqlite3.Connection, action_id: int,
                                     executed_on: str, config: dict) -> dict:
    """Mark a reviewed stage executed.  This is the only operation that locks it."""
    action_id = int(action_id)
    execution_day = _calendar_day(executed_on)
    with con:
        action = con.execute(
            "SELECT * FROM opportunity_stage_actions WHERE id=?", (action_id,)
        ).fetchone()
        if action is None:
            raise ValueError("机会档位记录不存在。")
        if action["status"] != "approved":
            raise ValueError("只有已批准的档位可标记为执行。")
        if config["opportunity_plan"]["require_risk_review"]:
            review, _ = _review_json(action["review_json"])
            if action["risk_status"] not in ("full", "half") or not action["manual_confirmed"] or not review:
                raise ValueError("本档尚未完成风险复核和人工确认。")
        linked = con.execute(
            "SELECT t.id,t.date,t.symbol,t.amount,t.fee FROM transactions t "
            "JOIN opportunity_action_transactions l ON l.transaction_id=t.id "
            "WHERE l.action_id=? ORDER BY t.date,t.id", (action_id,)
        ).fetchall()
        if not linked:
            raise ValueError("请先关联至少一笔实际发生的额外资金买入。")
        by_symbol = {symbol: Decimal(0) for symbol in SYMBOLS}
        for row in linked:
            by_symbol[row["symbol"]] += D(row["amount"])
        if any(by_symbol[symbol] <= 0 for symbol in SYMBOLS):
            raise ValueError("确认本档执行前，须同时关联 VOO 与 QQQM 的真实买入。")
        trade_total = sum(by_symbol.values(), Decimal(0))
        voo_pct = by_symbol["VOO"] / trade_total * 100
        if abs(voo_pct - Decimal("80")) > Decimal("2"):
            raise ValueError(
                f"本档真实买入应接近 VOO 80% / QQQM 20%；当前 VOO 为 {voo_pct:.1f}%。"
            )
        if execution_day < max(row["date"] for row in linked):
            raise ValueError("执行日期不可早于已关联买入日期。")
        actual = sum((D(row["amount"]) + D(row["fee"]) for row in linked), Decimal(0))
        if actual > D(action["approved_amount_usd"]) + Decimal("0.02"):
            raise ValueError("实际买入金额与费用合计超过本档批准上限。")
        existing = con.execute(
            "SELECT id FROM opportunity_stage_actions WHERE rule_hash=? AND cycle_key=? AND stage=? "
            "AND status='executed' AND id!=?",
            (action["rule_hash"], action["cycle_key"], action["stage"], action_id),
        ).fetchone()
        if existing is not None:
            raise ValueError("本轮该档位已经由另一记录执行。")
        con.execute(
            "UPDATE opportunity_stage_actions SET status='executed',executed_on=?,"
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (execution_day, action_id),
        )
    return next(
        item for item in list_opportunity_stage_actions(con, cycle_key=action["cycle_key"])
        if item["id"] == action_id
    )


def stage_execution_state(con: sqlite3.Connection, cycle_key: str,
                          rule_hash: str | None = None) -> dict:
    where = "cycle_key=? AND status='executed'"
    params: list[object] = [str(cycle_key)]
    if rule_hash is not None:
        where += " AND rule_hash=?"
        params.append(str(rule_hash))
    executed = [dict(row) for row in con.execute(
        "SELECT id,rule_hash,stage,trigger_symbol,market_date,executed_on,"
        "pool_usd_before,rule_amount_usd,effective_amount_usd,"
        "approved_amount_usd FROM opportunity_stage_actions WHERE " + where +
        " ORDER BY stage,id", params
    )]
    stages = sorted({int(row["stage"]) for row in executed})
    return {
        "cycle_key": str(cycle_key),
        "rule_hash": str(rule_hash) if rule_hash is not None else None,
        "executed_stages": stages,
        "highest_executed_stage": max(stages, default=0),
        "executed_actions": executed,
    }


def _validate_opportunity_transaction_references(con: sqlite3.Connection) -> None:
    """Keep edits to linked real transactions consistent with their opportunity records."""
    for row in con.execute(
        "SELECT e.id,e.event_type,t.kind,t.source_type FROM opportunity_cny_events e "
        "JOIN transactions t ON t.id=e.linked_transaction_id"
    ):
        expected = "extra" if row["event_type"] == "convert_to_usd" else (
            "fixed" if row["event_type"] == "excess_to_fixed" else None
        )
        if expected is None or row["kind"] != "deposit" or row["source_type"] != expected:
            raise ValueError(f"真实流水修改会使人民币机会资金事件 #{row['id']} 的关联失效。")
    totals: dict[int, Decimal] = {}
    for row in con.execute(
        "SELECT a.id AS action_id,a.market_date,a.approved_amount_usd,a.status,"
        "t.id AS transaction_id,t.date,t.kind,t.source_type,t.amount,t.fee "
        "FROM opportunity_stage_actions a "
        "JOIN opportunity_action_transactions l ON l.action_id=a.id "
        "JOIN transactions t ON t.id=l.transaction_id ORDER BY a.id,t.id"
    ):
        if row["kind"] != "buy" or row["source_type"] != "extra" or row["date"] < row["market_date"]:
            raise ValueError(
                f"真实流水修改会使机会档位 #{row['action_id']} 的买入关联失效。"
            )
        totals[row["action_id"]] = totals.get(row["action_id"], Decimal(0)) + D(row["amount"]) + D(row["fee"])
        if (row["approved_amount_usd"] is None or
                totals[row["action_id"]] > D(row["approved_amount_usd"]) + Decimal("0.02")):
            raise ValueError(f"机会档位 #{row['action_id']} 的关联买入超过批准上限。")


def validate_record(record: dict) -> dict:
    item = dict(record)
    item["date"] = parse_day(str(item["date"]))
    item["kind"] = str(item["kind"]).lower()
    if item["kind"] not in KINDS:
        raise ValueError("类型须为入金、出金、买入、卖出或分红。")
    item["symbol"] = (str(item.get("symbol") or "").upper() or None)
    item["amount"] = D(item["amount"])
    item["fee"] = D(item.get("fee") or 0)
    if item["amount"] <= 0 or item["fee"] < 0:
        raise ValueError("金额须大于零，手续费不可为负。")
    item["price"] = D(item["price"]) if item.get("price") not in (None, "") else None
    item["shares"] = D(item["shares"]) if item.get("shares") not in (None, "") else None
    item["note"] = str(item.get("note") or "")
    item["source_type"] = item.get("source_type") or None
    if item["source_type"] not in (None, "fixed", "extra"):
        raise ValueError("资金来源须为固定定投、额外资金或未分类。")
    item["plan_month"] = item.get("plan_month") or None
    if item["plan_month"] is not None and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", item["plan_month"]):
        raise ValueError("计划月份须为 YYYY-MM。")
    needs_plan_month = (
        item["source_type"] == "fixed" and item["kind"] in ("deposit", "buy")
    ) or (
        item["source_type"] == "extra" and item["kind"] == "deposit"
    )
    if needs_plan_month and not item["plan_month"]:
        label = "固定定投的入金及买入" if item["source_type"] == "fixed" else "额外资金入金"
        raise ValueError(f"{label}须填写计划月份。")
    if (item["kind"] == "buy" and item["source_type"] == "fixed"
            and item["plan_month"] and item["plan_month"] > item["date"][:7]):
        raise ValueError("买入归属的 VA 月份不能晚于实际成交月份。")
    if item["plan_month"] and not needs_plan_month:
        raise ValueError("计划月份只用于固定定投入金/买入或额外资金入金。")
    item["trigger_note"] = str(item.get("trigger_note") or "")
    if item["kind"] == "buy" and item["source_type"] == "extra" and not item["trigger_note"].strip():
        raise ValueError("额外资金买入须记录触发条件/复盘理由。")
    item["transfer_date"] = parse_day(item["transfer_date"]) if item.get("transfer_date") else None
    if item["transfer_date"] and item["transfer_date"] > item["date"]:
        raise ValueError("港币转出日期不可晚于美元到账日期。")
    for key in ("hkd_sent", "fx_rate_hkd_per_usd", "fx_cost_hkd"):
        item[key] = D(item[key]) if item.get(key) not in (None, "") else None
        if item[key] is not None and item[key] < 0:
            raise ValueError(f"{key} 不可为负。")
    if item["hkd_sent"] is not None and item["hkd_sent"] <= 0:
        raise ValueError("汇出港币须大于零。")
    if item["fx_rate_hkd_per_usd"] is not None and item["fx_rate_hkd_per_usd"] <= 0:
        raise ValueError("实际汇率须大于零。")
    if item["kind"] != "deposit" and any(item[k] is not None for k in ("transfer_date", "hkd_sent", "fx_rate_hkd_per_usd", "fx_cost_hkd")):
        raise ValueError("港币汇款字段仅用于入金。")
    if item["kind"] == "deposit" and item["hkd_sent"] is not None:
        if item["hkd_sent"] <= 0 or item["fx_rate_hkd_per_usd"] == 0:
            raise ValueError("汇出港币和汇率须大于零。")
        if item["fx_rate_hkd_per_usd"] is None:
            item["fx_rate_hkd_per_usd"] = item["hkd_sent"] / item["amount"]
    if item["kind"] == "deposit" and item["fee"] >= item["amount"]:
        raise ValueError("入金到账美元须大于美元账户另扣费用。")
    if item["kind"] in ("sell", "dividend") and item["fee"] >= item["amount"]:
        label = "卖出成交额" if item["kind"] == "sell" else "税前分红"
        raise ValueError(f"{label}须大于相关费用。")
    if item["kind"] in ("buy", "sell"):
        if item["symbol"] not in SYMBOLS or item["price"] is None or item["shares"] is None:
            raise ValueError("买卖记录需要标的、成交价和股数。")
        if item["price"] <= 0 or item["shares"] <= 0:
            raise ValueError("成交价和股数须大于零。")
        if abs(item["price"] * item["shares"] - item["amount"]) > Decimal("0.02"):
            raise ValueError("成交金额应等于成交价 × 股数，允许 0.02 美元的舍入差。")
    else:
        if item["price"] is not None or item["shares"] is not None:
            raise ValueError("资金与分红记录不填写成交价或股数。")
        if item["kind"] == "dividend" and item["symbol"] not in SYMBOLS:
            raise ValueError("分红记录需要 VOO 或 QQQM 标的。")
        if item["kind"] != "dividend" and item["symbol"] is not None:
            raise ValueError("入金和出金不填写标的。")
    return item


@dataclass
class Position:
    shares: Decimal = Decimal(0)
    basis: Decimal = Decimal(0)
    realized: Decimal = Decimal(0)
    dividend_gross: Decimal = Decimal(0)
    dividend_tax: Decimal = Decimal(0)
    dividends: Decimal = Decimal(0)
    dividend_count: int = 0
    last_dividend_date: str | None = None
    last_price: Decimal | None = None
    last_date: str | None = None


def calculate(transactions: list[sqlite3.Row], quotes: dict[str, sqlite3.Row]) -> dict:
    positions = {symbol: Position() for symbol in SYMBOLS}
    cash = deposits = withdrawals = buys = sales = fees = funding_fees = Decimal(0)
    by_month: dict[str, Decimal] = {}
    timeline=[(r['date'], int(r['id']), r) for r in transactions]
    timeline += [(r['date'], -int(r['id']), {'kind':'split', **dict(r)}) for r in getattr(transactions,'splits',[])]
    for _,_,row in sorted(timeline,key=lambda item:(item[0],item[1])):
        if row['kind']=='split':
            p=positions[row['symbol']];ratio=D(row['ratio']);p.shares*=ratio
            if p.last_price is not None:p.last_price/=ratio
            p.last_date=row['date']
            continue
        kind, amount, fee = row["kind"], D(row["amount"]), D(row["fee"])
        symbol = row["symbol"]
        fees += fee
        if kind == "deposit":
            deposits += amount
            funding_fees += fee
            cash += amount - fee
        elif kind == "withdrawal":
            withdrawals += amount
            funding_fees += fee
            cash -= amount + fee
        elif kind == "dividend":
            position = positions[symbol]
            position.dividend_gross += amount
            position.dividend_tax += fee
            position.dividends += amount - fee
            position.dividend_count += 1
            if position.last_dividend_date is None or row["date"] >= position.last_dividend_date:
                position.last_dividend_date = row["date"]
            cash += amount - fee
        else:
            p, qty, price = positions[symbol], D(row["shares"]), D(row["price"])
            p.last_price, p.last_date = price, row["date"]
            if kind == "buy":
                buys += amount + fee
                cash -= amount + fee
                p.shares += qty
                p.basis += amount + fee
                month = row["date"][:7]
                by_month[month] = by_month.get(month, Decimal(0)) + amount + fee
            else:
                if qty > p.shares + Decimal("0.000000000001"):
                    raise ValueError(f"{row['date']} {symbol} 卖出股数超过当时持仓；请检查记录 #{row['id']}。")
                used_basis = p.basis * qty / p.shares
                p.realized += amount - fee - used_basis
                p.shares -= qty
                p.basis -= used_basis
                sales += amount - fee
                cash += amount - fee
                if abs(p.shares) < Decimal("0.000000000001"):
                    p.shares = p.basis = Decimal(0)
    valuations = {}
    total_value = Decimal(0)
    for symbol, p in positions.items():
        quote = quotes.get(symbol)
        use_quote = bool(quote and (not p.last_date or quote["as_of"] >= p.last_date))
        price = D(quote["price"]) if use_quote else p.last_price
        as_of = quote["as_of"] if use_quote else p.last_date
        if use_quote:
            quote_source = quote["source"] if "source" in quote.keys() else "manual"
            source_key = quote_source if quote_source in QUOTE_SOURCE_LABELS else "manual"
            source = QUOTE_SOURCE_LABELS[source_key]
        else:
            source_key = "trade" if price is not None else "missing"
            source = "最近成交价" if price is not None else "未录入"
        value = p.shares * price if price is not None else Decimal(0)
        total_value += value
        valuations[symbol] = {"price": price, "date": as_of, "source": source,
                              "source_key": source_key,
                              "value": value, "unrealized": value - p.basis if price is not None else None}
    realized_sales = sum((p.realized for p in positions.values()), Decimal(0))
    dividend_gross = sum((p.dividend_gross for p in positions.values()), Decimal(0))
    dividend_tax = sum((p.dividend_tax for p in positions.values()), Decimal(0))
    dividends = sum((p.dividends for p in positions.values()), Decimal(0))
    dividend_count = sum(p.dividend_count for p in positions.values())
    unrealized = total_value - sum((p.basis for p in positions.values()), Decimal(0))
    return {"positions": positions, "valuations": valuations, "cash": cash,
            "deposits": deposits, "withdrawals": withdrawals, "net_contributed": deposits - withdrawals,
            "buys": buys, "sales": sales, "fees": fees, "funding_fees": funding_fees,
            "realized_sales": realized_sales,
            "dividend_gross": dividend_gross, "dividend_tax": dividend_tax,
            "dividend_count": dividend_count, "dividends": dividends,
            "realized_total": realized_sales + dividends,
            "market_value": total_value, "account_value": total_value + cash,
            "unrealized": unrealized,
            "total_pnl": realized_sales + dividends + unrealized - funding_fees,
            "by_month": by_month}


def _month_index(start_month: str, current_month: str) -> int:
    sy, sm = map(int, start_month.split("-"))
    cy, cm = map(int, current_month.split("-"))
    return (cy - sy) * 12 + (cm - sm)


def va_targets_for_month(config: dict, plan_month: str) -> dict[str, Decimal]:
    """Return independent VOO/QQQM VA target values for one month."""
    month = _month_text(plan_month, "VA 计划月份")
    plan = config["va_plan"]
    offset = _month_index(plan["start_month"], month)
    if offset < 0:
        return {symbol: Decimal(0) for symbol in SYMBOLS}
    return {
        symbol: D(plan["initial_targets_usd"][symbol]) + D(plan["monthly_steps_usd"][symbol]) * offset
        for symbol in SYMBOLS
    }


def va_budget_date(config: dict, plan_month: str) -> date:
    """Return the local calendar date on which one month's VA budget is fixed."""
    month = _month_text(plan_month, "VA 计划月份")
    year, value = map(int, month.split("-"))
    return date(year, value, int(config["budget_day"]))


def va_budget_market_quotes(config: dict, plan_month: str) -> tuple[dict[str, dict], str]:
    """Load the last complete U.S. close available before the local budget day.

    The tool runs in Asia/Shanghai.  On the morning of local day 2, the latest
    complete U.S. session normally carries day 1's market date.  Reading the
    historical price file makes a delayed lock reproducible instead of silently
    substituting a later quote.
    """
    budget = va_budget_date(config, plan_month)
    from market_prices import resolve
    source = resolve(Path(config["database_path"]).parent / "source_adjusted_close.csv", "raw", required=True)
    if not source.is_file():
        raise ValueError("缺少历史行情文件，请先运行“更新行情并回测”，再锁定本月预算。")
    selected: tuple[date, dict[str, Decimal]] | None = None
    with source.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            try:
                market_day = date.fromisoformat(str(row.get("Date") or ""))
                prices = {symbol: D(row.get(symbol)) for symbol in SYMBOLS}
            except (ValueError, TypeError):
                continue
            if market_day >= budget or any(price <= 0 for price in prices.values()):
                continue
            if selected is None or market_day > selected[0]:
                selected = (market_day, prices)
    if selected is None:
        raise ValueError(
            f"没有找到 {budget.isoformat()} 预算日前的完整 VOO / QQQM 行情，请先更新行情。"
        )
    market_day, prices = selected
    try:
        import exchange_calendars as xcals
        import pandas as pd
        calendar = xcals.get_calendar("XNYS")
        expected = calendar.date_to_session(pd.Timestamp(budget) - pd.Timedelta(days=1), direction="previous").date()
        if calendar.session_close(pd.Timestamp(expected)) > pd.Timestamp.now(tz="UTC"):
            raise ValueError("预算所需美股交易日尚未收盘，请在收盘后更新行情再锁定。")
    except ImportError as exc:
        raise ValueError("缺少美股交易日历依赖，请安装 requirements.txt 后再锁定预算。") from exc
    if market_day != expected:
        raise ValueError(f"预算需要 {expected.isoformat()} 的完整行情，当前只有 {market_day.isoformat()}；请先更新行情，不能使用较旧价格锁定预算。")
    if (budget - market_day).days > 7:
        raise ValueError(
            f"预算日参考行情停留在 {market_day.isoformat()}，时间过旧；请先更新行情。"
        )
    quotes = {
        symbol: {
            "symbol": symbol,
            "as_of": market_day.isoformat(),
            "price": decimal_text(prices[symbol]),
            "source": "market",
        }
        for symbol in SYMBOLS
    }
    return quotes, market_day.isoformat()


def va_summary(state: dict, config: dict, *, today: date | None = None) -> dict:
    """Calculate the real-account independent Value Averaging plan.

    October 2026 uses the configured initial targets.  From the following month
    each symbol's target increases independently by its configured monthly step.
    Every gap, including the first VA month, uses current reference market value.
    Cost basis is reported separately and never participates in the VA gap.  No
    automatic selling is performed.
    """
    today = today or date.today()
    month = today.strftime("%Y-%m")
    plan = config["va_plan"]
    active = month >= plan["start_month"]
    plan_month = month if active else plan["start_month"]
    offset = _month_index(plan["start_month"], plan_month)
    targets = va_targets_for_month(config, plan_month)
    rows = {}
    raw_total = Decimal(0)
    for symbol in SYMBOLS:
        pos = state["positions"][symbol]
        val = state["valuations"][symbol]
        current_market = val["value"]
        # Independent VA always measures the target shortfall against the value
        # of the shares at the current reference price.  Historical cost is useful
        # for P&L reporting only; using it here would suppress market movements.
        measurement = current_market
        target = targets[symbol]
        gap = max(Decimal(0), target - measurement)
        raw_total += gap
        rows[symbol] = {
            "symbol": symbol,
            "target_value_usd": target,
            "current_market_value_usd": current_market,
            "current_cost_basis_usd": pos.basis,
            "measurement_value_usd": measurement,
            "measurement_basis": "market_value",
            "raw_gap_usd": gap,
            "reference_price_usd": val["price"],
            "reference_price_date": val["date"],
        }
    # VOO and QQQM are fully independent VA paths.  There is no combined
    # monthly cap and no cross-symbol scaling.  Each symbol simply buys its
    # entire positive target-value gap; if it is already at/above target, buy 0.
    suggested_total = Decimal(0)
    for symbol in SYMBOLS:
        suggested = rows[symbol]["raw_gap_usd"].quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        rows[symbol]["suggested_buy_usd"] = suggested
        suggested_total += suggested
    return {
        "mode": "va",
        "active": active,
        "current_month": month,
        "month": plan_month,
        "start_month": plan["start_month"],
        "preview": not active,
        "month_index": offset,
        "initial_targets_usd": {s: D(plan["initial_targets_usd"][s]) for s in SYMBOLS},
        "monthly_steps_usd": {s: D(plan["monthly_steps_usd"][s]) for s in SYMBOLS},
        "raw_required_usd": raw_total,
        "suggested_total_usd": suggested_total,
        "sell_if_above_target": bool(plan.get("sell_if_above_target", False)),
        "symbols": rows,
    }


def va_calendar_checkpoints(con: sqlite3.Connection) -> dict[tuple[str, str], sqlite3.Row]:
    """Return the frozen execution plan for each VA month and symbol."""
    return {
        (row["plan_month"], row["symbol"]): row
        for row in con.execute(
            "SELECT * FROM va_month_checkpoints ORDER BY plan_month,symbol"
        )
    }


def va_fixed_buy_activity(
    transactions: list[sqlite3.Row],
) -> dict[tuple[str, str], dict]:
    """Aggregate real fixed-plan buys by their explicit VA month.

    The execution amount deliberately excludes fees.  Fees remain part of the
    account cost basis and cash ledger, but they do not buy ETF market value.
    """
    result: dict[tuple[str, str], dict] = {}
    for row in transactions:
        if (row["kind"] != "buy" or row["source_type"] != "fixed"
                or not row["plan_month"] or row["symbol"] not in SYMBOLS):
            continue
        key = (row["plan_month"], row["symbol"])
        item = result.setdefault(key, {
            "amount": Decimal(0), "fees": Decimal(0), "shares": Decimal(0),
            "first_date": None, "last_date": None, "trades": [],
        })
        amount, fee, shares = D(row["amount"]), D(row["fee"]), D(row["shares"])
        item["amount"] += amount
        item["fees"] += fee
        item["shares"] += shares
        item["first_date"] = min(item["first_date"] or row["date"], row["date"])
        item["last_date"] = max(item["last_date"] or row["date"], row["date"])
        item["trades"].append({
            "id": int(row["id"]), "date": row["date"], "amount": amount,
        })
    for item in result.values():
        item["trades"].sort(key=lambda trade: (trade["date"], trade["id"]))
    return result


def va_completion_date(activity: dict | None, required_buy: Decimal) -> str | None:
    """Return the first trade date on which the frozen requirement was met."""
    if required_buy <= Decimal("0.005"):
        return None
    running = Decimal(0)
    for trade in (activity or {}).get("trades", []):
        running += trade["amount"]
        if running + Decimal("0.02") >= required_buy:
            return trade["date"]
    return None


def va_over_budget_tolerance(required_buy: Decimal) -> Decimal:
    """Ignore immaterial execution rounding when classifying budget status."""
    required = max(Decimal(0), D(required_buy))
    return max(Decimal("1.00"), required * Decimal("0.01"))


def capture_va_month_checkpoints(
    con: sqlite3.Connection,
    config: dict,
    plan_month: str,
    *,
    evaluated_on: str | None = None,
    freeze_reason: str = "manual",
    transactions: list[sqlite3.Row] | None = None,
) -> list[str]:
    """Freeze one month's budget-day VA requirement without changing trades.

    Existing checkpoint rows are immutable here.  This makes later quote and
    configuration changes unable to rewrite a decision that was already acted
    on.  Actual execution remains derived from the transaction ledger, so an
    edited or deleted trade updates completion immediately.
    """
    month = _month_text(plan_month, "VA 计划月份")
    if month < config["va_plan"]["start_month"]:
        raise ValueError("不能在 VA 计划开始月份之前锁定月度预算。")
    if freeze_reason not in {"manual", "first_buy", "existing_buys"}:
        raise ValueError("VA 月度方案的冻结原因不正确。")
    evaluated = parse_day(evaluated_on or date.today().isoformat())
    budget = va_budget_date(config, month)
    if date.fromisoformat(evaluated) < budget:
        raise ValueError(
            f"{month} 的预算确定日是 {budget.isoformat()}；到当天再锁定本月预算。"
        )
    source_rows = transactions if transactions is not None else rows(con)
    activity = va_fixed_buy_activity(source_rows)
    if pending_stock_splits(con,config):
        raise ValueError("存在尚未核对的持仓拆股，请先完成拆股核对再计算预算。")
    reference_rows = LedgerRows([row for row in source_rows if row["date"] < budget.isoformat()],
                               [row for row in getattr(source_rows,'splits',[]) if row['date'] < budget.isoformat()])
    reference_quotes, reference_market_day = va_budget_market_quotes(config, month)
    state = calculate(reference_rows, reference_quotes)
    targets = va_targets_for_month(config, month)
    inserted: list[str] = []
    for symbol in SYMBOLS:
        if con.execute(
            "SELECT 1 FROM va_month_checkpoints WHERE plan_month=? AND symbol=?",
            (month, symbol),
        ).fetchone() is not None:
            continue
        position = state["positions"][symbol]
        valuation = state["valuations"][symbol]
        if position.shares > 0 and valuation["price"] is None:
            raise ValueError(f"{symbol} 尚无有效参考价格，不能锁定本月 VA 预算。")
        actual = activity.get((month, symbol), {}).get("amount", Decimal(0))
        current_market = valuation["value"]
        required = max(Decimal(0), targets[symbol] - current_market).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        reason = "existing_buys" if actual > 0 and freeze_reason == "manual" else freeze_reason
        note_parts = [
            f"预算日 {budget.isoformat()}；采用此前最近完整美股收盘日 {reference_market_day}。"
        ]
        if evaluated > budget.isoformat():
            note_parts.append(f"实际于 {evaluated} 补锁预算。")
        if actual > 0:
            note_parts.append("锁定前已有当月买入；完成度仍按真实流水重算。")
        note = "".join(note_parts)
        con.execute(
            "INSERT INTO va_month_checkpoints("
            "plan_month,symbol,target_value_usd,required_buy_usd,"
            "reference_market_value_usd,reference_shares,reference_price_usd,"
            "reference_price_date,reference_price_source,budget_date,evaluated_on,"
            "freeze_reason,note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                month, symbol, decimal_text(targets[symbol]), decimal_text(required),
                decimal_text(current_market), decimal_text(position.shares),
                (decimal_text(valuation["price"])
                 if valuation["price"] is not None else None),
                valuation["date"], valuation["source"], budget.isoformat(),
                evaluated, reason, note,
            ),
        )
        inserted.append(symbol)
    return inserted


def confirm_va_month(
    con: sqlite3.Connection,
    config: dict,
    plan_month: str,
    *,
    today: date | None = None,
) -> list[str]:
    """Lock the active month's VA budget on or after its configured budget day."""
    today = today or date.today()
    month = _month_text(plan_month, "VA 计划月份")
    current_month = today.strftime("%Y-%m")
    if current_month < config["va_plan"]["start_month"]:
        raise ValueError(
            f"VA 计划将在 {config['va_plan']['start_month']} 开始，届时才能锁定月度预算。"
        )
    if month != current_month:
        raise ValueError("只能锁定当前月份的 VA 预算。")
    budget = va_budget_date(config, month)
    if today < budget:
        raise ValueError(
            f"本月预算将在 {budget.isoformat()} 确定；预算日前仅显示动态预估缺口。"
        )
    _daily_backup_before_mutation(con)
    with con:
        return capture_va_month_checkpoints(
            con, config, month, evaluated_on=today.isoformat(), freeze_reason="manual"
        )


def va_budget_preview(con: sqlite3.Connection, config: dict, plan_month: str) -> dict:
    """Compute an auditable proposal in memory without changing the real ledger."""
    month = _month_text(plan_month, "VA 计划月份")
    if month != date.today().strftime("%Y-%m"):
        raise ValueError("只能核对当前月份的预算。")
    if date.today() < va_budget_date(config, month):
        raise ValueError("尚未到本月预算日。")
    memory = sqlite3.connect(":memory:")
    memory.row_factory = sqlite3.Row
    try:
        con.backup(memory)
        memory.execute("DELETE FROM va_month_checkpoints WHERE plan_month=?", (month,))
        capture_va_month_checkpoints(memory, config, month)
        proposal = [dict(row) for row in memory.execute(
            "SELECT * FROM va_month_checkpoints WHERE plan_month=? ORDER BY symbol", (month,)
        )]
        # Creation timestamps are incidental to the proposal.
        for row in proposal:
            row.pop("created_at", None)
            row.pop("updated_at", None)
        existing = [dict(row) for row in con.execute(
            "SELECT * FROM va_month_checkpoints WHERE plan_month=? ORDER BY symbol", (month,)
        )]
        payload = {"proposal": proposal, "existing": existing,
                   "transactions": [dict(row) for row in rows(con)], "splits": [dict(row) for row in rows(con).splits], "plan": config["va_plan"]}
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        return {"month": month, "symbols": proposal, "fingerprint": fingerprint,
                "already_locked": bool(existing)}
    finally:
        memory.close()


def va_budget_reference_warnings(con: sqlite3.Connection, transactions: list[sqlite3.Row]) -> list[str]:
    warnings = []
    for checkpoint in con.execute("SELECT * FROM va_month_checkpoints ORDER BY plan_month,symbol"):
        boundary = checkpoint["budget_date"]
        earlier=LedgerRows([r for r in transactions if r['date']<boundary],
                           [r for r in getattr(transactions,'splits',[]) if r['date']<boundary])
        qty=calculate(earlier,{})['positions'][checkpoint['symbol']].shares
        if abs(qty - D(checkpoint["reference_shares"])) > Decimal("0.00000001"):
            warnings.append(f"{checkpoint['plan_month']} {checkpoint['symbol']}：历史流水已更正，预算参考股数 {checkpoint['reference_shares']} 与当前重建股数 {decimal_text(qty)} 不一致，请复核预算。")
    return warnings


def apply_va_budget_proposal(con: sqlite3.Connection, config: dict, month: str,
                             fingerprint: str, *, recalculate: bool = False) -> list[str]:
    proposal = va_budget_preview(con, config, month)
    if fingerprint != proposal["fingerprint"]:
        raise ValueError("行情、流水或预算参数已变化，请重新预览预算后确认。")
    if not recalculate:
        return confirm_va_month(con, config, month)
    _daily_backup_before_mutation(con)
    with con:
        before = [dict(row) for row in con.execute("SELECT * FROM va_month_checkpoints WHERE plan_month=?", (month,))]
        if not before:
            raise ValueError("本月尚未锁定，请使用锁定预算。")
        con.execute("CREATE TABLE IF NOT EXISTS va_budget_revisions (id INTEGER PRIMARY KEY, plan_month TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        con.execute("DELETE FROM va_month_checkpoints WHERE plan_month=?", (month,))
        inserted = capture_va_month_checkpoints(con, config, month)
        after = [dict(row) for row in con.execute("SELECT * FROM va_month_checkpoints WHERE plan_month=?", (month,))]
        con.execute("INSERT INTO va_budget_revisions(plan_month,before_json,after_json) VALUES (?,?,?)",
                    (month, json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False)))
        return inserted


def va_execution_summary(
    con: sqlite3.Connection,
    config: dict,
    *,
    transactions: list[sqlite3.Row] | None = None,
    state: dict | None = None,
    today: date | None = None,
) -> dict:
    """Return the current budget lock and month-to-date execution state."""
    today = today or date.today()
    current_month = today.strftime("%Y-%m")
    start_month = config["va_plan"]["start_month"]
    active = current_month >= start_month
    month = current_month if active else start_month
    source_rows = transactions if transactions is not None else rows(con)
    current_state = state if state is not None else calculate(source_rows, get_quotes(con))
    checkpoints = va_calendar_checkpoints(con)
    activity = va_fixed_buy_activity(source_rows)
    targets = va_targets_for_month(config, month)
    budget = va_budget_date(config, month)
    symbols: dict[str, dict] = {}
    confirmed = True
    for symbol in SYMBOLS:
        checkpoint = checkpoints.get((month, symbol))
        execution = activity.get((month, symbol))
        actual = (execution or {}).get("amount", Decimal(0)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if checkpoint is None:
            confirmed = False
            live_gap = max(
                Decimal(0), targets[symbol] - current_state["valuations"][symbol]["value"]
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            symbols[symbol] = {
                "target_value_usd": targets[symbol],
                "locked_budget_usd": None,
                "actual_buy_usd": actual,
                "remaining_usd": None,
                "overage_usd": Decimal(0),
                "completion_pct": None,
                "live_gap_usd": live_gap,
                "reference_market_value_usd": None,
                "reference_price_usd": None,
                "reference_price_date": None,
                "reference_price_source": None,
                "budget_date": budget.isoformat(),
                "locked_on": None,
            }
            continue
        required = D(checkpoint["required_buy_usd"])
        remaining = max(Decimal(0), required - actual).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        overage = max(Decimal(0), actual - required).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        completion = (
            Decimal(100) if required <= Decimal("0.005")
            else min(Decimal(100), actual / required * 100)
        )
        symbols[symbol] = {
            "target_value_usd": D(checkpoint["target_value_usd"]),
            "locked_budget_usd": required,
            "actual_buy_usd": actual,
            "remaining_usd": remaining,
            "overage_usd": overage,
            "completion_pct": completion,
            "live_gap_usd": max(
                Decimal(0), targets[symbol] - current_state["valuations"][symbol]["value"]
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            "reference_market_value_usd": D(checkpoint["reference_market_value_usd"]),
            "reference_price_usd": (
                D(checkpoint["reference_price_usd"])
                if checkpoint["reference_price_usd"] is not None else None
            ),
            "reference_price_date": checkpoint["reference_price_date"],
            "reference_price_source": checkpoint["reference_price_source"],
            "budget_date": checkpoint["budget_date"] or budget.isoformat(),
            "locked_on": checkpoint["evaluated_on"],
        }
    locked_values = [
        symbols[symbol]["locked_budget_usd"] for symbol in SYMBOLS
        if symbols[symbol]["locked_budget_usd"] is not None
    ]
    actual_total = sum(
        (symbols[symbol]["actual_buy_usd"] for symbol in SYMBOLS), Decimal(0)
    )
    locked_total = sum(locked_values, Decimal(0)) if len(locked_values) == len(SYMBOLS) else None
    remaining_total = (
        sum((symbols[symbol]["remaining_usd"] for symbol in SYMBOLS), Decimal(0))
        if confirmed else None
    )
    overage_total = sum(
        (symbols[symbol]["overage_usd"] for symbol in SYMBOLS), Decimal(0)
    )
    materially_over_budget = any(
        symbols[symbol]["locked_budget_usd"] is not None
        and symbols[symbol]["actual_buy_usd"]
            > symbols[symbol]["locked_budget_usd"]
              + va_over_budget_tolerance(symbols[symbol]["locked_budget_usd"])
        for symbol in SYMBOLS
    )
    if not active:
        status = "future"
    elif not confirmed:
        status = "waiting_budget_day" if today < budget else "awaiting_budget_lock"
    elif materially_over_budget:
        status = "over_budget"
    elif locked_total is not None and locked_total <= Decimal("0.005"):
        status = "no_buy_needed"
    elif remaining_total is not None and remaining_total <= Decimal("0.02"):
        status = "completed"
    elif actual_total > 0:
        status = "partial" if month == current_month else "incomplete"
    else:
        status = "ready_to_execute" if month == current_month else "incomplete"
    completion_total = None
    if locked_total is not None:
        completion_total = (
            Decimal(100) if locked_total <= Decimal("0.005")
            else min(Decimal(100), actual_total / locked_total * 100)
        )
    return {
        "active": active,
        "month": month,
        "budget_day": int(config["budget_day"]),
        "budget_date": (symbols[SYMBOLS[0]]["budget_date"] if confirmed else budget.isoformat()),
        "confirmed": confirmed,
        "status": status,
        "locked_on": next(
            (symbols[symbol]["locked_on"] for symbol in SYMBOLS if symbols[symbol]["locked_on"]),
            None,
        ),
        "locked_budget_usd": locked_total,
        "actual_buy_usd": actual_total,
        "remaining_usd": remaining_total,
        "overage_usd": overage_total,
        "completion_pct": completion_total,
        "live_gap_usd": sum(
            (symbols[symbol]["live_gap_usd"] for symbol in SYMBOLS), Decimal(0)
        ),
        "symbols": symbols,
    }


def va_target_weights(va: dict) -> dict[str, Decimal]:
    """Return the current month's target-value mix for display only."""
    targets = {
        symbol: D(va["symbols"][symbol]["target_value_usd"]) for symbol in SYMBOLS
    }
    total = sum(targets.values(), Decimal(0))
    return {
        symbol: (targets[symbol] / total * 100 if total else Decimal(0))
        for symbol in SYMBOLS
    }


def plan_and_pool(transactions: list[sqlite3.Row], config: dict, today: date | None = None) -> dict:
    """Classified cash and plan metrics. Historical NULL sources remain unclassified."""
    today = today or date.today()
    month = today.strftime("%Y-%m")
    current_budget = monthly_budget_for(config, month)
    fixed_buys: dict[str, Decimal] = {}
    fixed_arrivals: dict[str, str] = {}
    fixed_trades: dict[str, str] = {}
    pool = {"fixed": Decimal(0), "extra": Decimal(0), "legacy": Decimal(0)}
    extra_deposits = extra_buys = Decimal(0)
    extra_deposits_by_month: dict[str, Decimal] = {}
    extra_arrivals: dict[str, str] = {}
    lots: list[dict] = []
    deposit_usage: dict[int, dict] = {}
    for r in transactions:
        source = r["source_type"] or "legacy"
        kind, amount, fee = r["kind"], D(r["amount"]), D(r["fee"])
        delta = amount - fee if kind in ("deposit", "sell", "dividend") else -amount - fee
        pool[source] += delta
        if kind == "deposit":
            if source == "extra":
                extra_deposits += amount - fee
                key = r["plan_month"] or r["date"][:7]
                extra_deposits_by_month[key] = (
                    extra_deposits_by_month.get(key, Decimal(0)) + amount - fee
                )
                extra_arrivals[key] = min(extra_arrivals.get(key, r["date"]), r["date"])
            lots.append({"id": r["id"], "source": source, "remaining": amount - fee, "date": r["date"]})
            deposit_usage[r["id"]] = {"amount": amount - fee, "remaining": amount - fee,
                                      "invested": Decimal(0), "withdrawn": Decimal(0)}
            if source == "fixed":
                key = r["plan_month"] or r["date"][:7]
                fixed_arrivals[key] = min(fixed_arrivals.get(key, r["date"]), r["date"])
        elif kind in ("sell", "dividend"):
            if amount > fee:
                lots.append({"id": None, "source": source, "remaining": amount - fee, "date": r["date"]})
        elif kind in ("buy", "withdrawal"):
            if kind == "buy" and source == "extra":
                extra_buys += amount + fee
            if kind == "buy" and source == "fixed":
                key = r["plan_month"] or r["date"][:7]
                fixed_buys[key] = fixed_buys.get(key, Decimal(0)) + amount + fee
                fixed_trades[key] = max(fixed_trades.get(key, r["date"]), r["date"])
            remaining = amount + fee
            for lot in lots:
                if lot["source"] != source or remaining <= 0:
                    continue
                used = min(lot["remaining"], remaining)
                lot["remaining"] -= used
                if lot["id"] is not None:
                    deposit_usage[lot["id"]]["remaining"] -= used
                    deposit_usage[lot["id"]]["invested" if kind == "buy" else "withdrawn"] += used
                remaining -= used
    idle_extra_lots = [lot for lot in lots if lot["source"] == "extra" and lot["remaining"] > 0]
    oldest = min((lot["date"] for lot in idle_extra_lots), default=None)
    wait_usd_days = sum((lot["remaining"] * max(0, (today - date.fromisoformat(lot["date"])).days)
                         for lot in idle_extra_lots), Decimal(0))
    idle_extra_cash = sum((lot["remaining"] for lot in idle_extra_lots), Decimal(0))
    start = config.get("monthly_plan_start") or min(fixed_buys.keys() | fixed_arrivals.keys(), default=month)
    eligible = []
    y, m = map(int, start.split("-"))
    while f"{y:04d}-{m:02d}" <= month:
        candidate = f"{y:04d}-{m:02d}"
        if candidate < month or today.day >= int(config.get("budget_day", 2)):
            eligible.append(candidate)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    completed = sum(
        fixed_buys.get(candidate, Decimal(0)) >= monthly_budget_for(config, candidate)
        for candidate in eligible
    )
    return {
        "pool": pool, "extra_deposits": extra_deposits, "extra_buys": extra_buys,
        "extra_deposits_by_month": extra_deposits_by_month,
        "extra_arrivals": extra_arrivals,
        "extra_usage_pct": extra_buys / extra_deposits * 100 if extra_deposits else Decimal(0),
        "extra_wait_days": max(0, (today - date.fromisoformat(oldest)).days) if oldest else 0,
        "extra_wait_usd_days": wait_usd_days,
        "extra_avg_wait_days": wait_usd_days / idle_extra_cash if idle_extra_cash else Decimal(0),
        "extra_oldest_date": oldest, "deposit_usage": deposit_usage,
        "fixed_buys": fixed_buys, "fixed_arrivals": fixed_arrivals, "fixed_trades": fixed_trades,
        "current_month": month, "current_budget": current_budget,
        "current_fixed_buy": fixed_buys.get(month, Decimal(0)),
        "current_completion_pct": min(
            Decimal(100), fixed_buys.get(month, Decimal(0)) / current_budget * 100
        ),
        "completed_months": completed, "eligible_months": len(eligible),
        "fixed_completion_pct": Decimal(completed) / len(eligible) * 100 if eligible else Decimal(0),
    }


def opportunity_usd_summary(con: sqlite3.Connection, config: dict,
                            today: date | None = None,
                            transactions: list[sqlite3.Row] | None = None) -> dict:
    """Summarize the opportunity plan from the real ``extra`` USD ledger."""
    today = today or date.today()
    month = today.strftime("%Y-%m")
    plan = config["opportunity_plan"]
    start_month = plan["start_month"]
    monthly_target = D(plan["monthly_usd"])
    cap = D(plan["cap_usd"])
    tx_rows = transactions if transactions is not None else rows(con)
    metrics = plan_and_pool(tx_rows, config, today)
    active = month >= start_month
    monthly_actual = metrics["extra_deposits_by_month"].get(month, Decimal(0))
    balance = max(Decimal(0), metrics["pool"]["extra"])
    deployable = min(balance, cap)
    scheduled_months = 0
    if active:
        sy, sm = map(int, start_month.split("-"))
        ey, em = map(int, month.split("-"))
        scheduled_months = (ey - sy) * 12 + em - sm + 1
    cumulative_actual = sum(
        (amount for key, amount in metrics["extra_deposits_by_month"].items()
         if start_month <= key <= month),
        Decimal(0),
    )
    executed_count = con.execute(
        "SELECT COUNT(*) FROM opportunity_stage_actions WHERE status='executed'"
    ).fetchone()[0]
    return {
        "current_month": month,
        "start_month": start_month,
        "active": active,
        "monthly_target_usd": monthly_target,
        "monthly_actual_usd": monthly_actual,
        "monthly_remaining_usd": (
            max(Decimal(0), monthly_target - monthly_actual) if active else Decimal(0)
        ),
        "monthly_overage_usd": (
            max(Decimal(0), monthly_actual - monthly_target) if active else Decimal(0)
        ),
        "monthly_completion_pct": (
            min(Decimal(100), monthly_actual / monthly_target * 100) if active else Decimal(0)
        ),
        "scheduled_months": scheduled_months,
        "cumulative_target_usd": monthly_target * scheduled_months,
        "cumulative_actual_usd": cumulative_actual,
        "balance_usd": balance,
        "cap_usd": cap,
        "deployable_usd": deployable,
        "excess_usd": max(Decimal(0), balance - cap),
        "current_arrival": metrics["extra_arrivals"].get(month),
        "executed_stage_count": int(executed_count),
    }


def evaluate_rules(observation: dict, rules: dict) -> dict:
    """Describe manually observed conditions; this is never a trade instruction."""
    checks = {}
    pairs = (("min_52w_drawdown_pct", "drawdown_52w_pct", "min"),
             ("min_vix", "vix", "min"),
             ("min_10y_yield_pct", "yield_10y_pct", "min"),
             ("max_10y_yield_pct", "yield_10y_pct", "max"))
    for threshold, field, direction in pairs:
        if rules.get(threshold) is not None:
            value = observation.get(field)
            checks[threshold] = (None if value in (None, "") else
                                 (D(value) >= D(rules[threshold]) if direction == "min" else D(value) <= D(rules[threshold])))
    for span in (20, 50, 200):
        setting = rules.get(f"ma{span}_position", "any")
        if setting != "any":
            observed = observation.get(f"ma{span}_position")
            checks[f"ma{span}_position"] = None if not observed else observed == setting
    if rules.get("require_valuation_note"):
        checks["valuation_note"] = bool(str(observation.get("valuation_note") or "").strip())
    if rules.get("require_manual_confirmation"):
        checks["manual_confirmation"] = bool(observation.get("manual_confirmation"))
    if not checks:
        status = "未配置条件"
    else:
        status = "资料不足" if any(value is None for value in checks.values()) else ("供人工复核" if all(checks.values()) else "未满足所设条件")
    return {"status": status, "checks": checks}


def get_quotes(con: sqlite3.Connection) -> dict:
    return {row["symbol"]: row for row in con.execute("SELECT * FROM quotes")}


def latest_market_prices(price_csv: Path, expected_as_of: str | None = None) -> dict:
    """Read the last complete VOO/QQQM row from the already validated price file."""
    price_csv = Path(price_csv).expanduser()
    if not price_csv.is_file():
        raise FileNotFoundError(f"找不到已校验的行情文件：{price_csv}")
    latest: dict | None = None
    previous_day: date | None = None
    with price_csv.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {"Date", *SYMBOLS}.issubset(reader.fieldnames):
            raise ValueError("行情文件缺少 Date、VOO 或 QQQM 列，未更新持仓估算价。")
        for line_number, row in enumerate(reader, start=2):
            try:
                market_day = date.fromisoformat(str(row.get("Date") or ""))
                prices = {symbol: D(row.get(symbol)) for symbol in SYMBOLS}
            except (ValueError, TypeError) as exc:
                raise ValueError(f"行情文件第 {line_number} 行无效，未更新持仓估算价。") from exc
            if previous_day is not None and market_day <= previous_day:
                raise ValueError("行情文件日期未严格递增，未更新持仓估算价。")
            if any(price <= 0 for price in prices.values()):
                raise ValueError(f"行情文件第 {line_number} 行含非正价格，未更新持仓估算价。")
            previous_day = market_day
            latest = {"as_of": market_day.isoformat(), "prices": prices}
    if latest is None:
        raise ValueError("行情文件没有有效价格，未更新持仓估算价。")
    if expected_as_of is not None and latest["as_of"] != expected_as_of:
        raise ValueError(
            f"行情文件最新日期 {latest['as_of']} 与本次回测日期 {expected_as_of} 不一致，"
            "未更新持仓估算价。"
        )
    return latest


def sync_market_quotes(con: sqlite3.Connection, price_csv: Path,
                       expected_as_of: str | None = None) -> dict:
    """Atomically copy the latest validated market row into valuation quotes.

    A newer manual quote is retained so an older completed daily bar can never move
    a holding's estimate date backwards.
    """
    database = next((r[2] for r in con.execute("PRAGMA database_list") if r[1] == "main"), "")
    if database and pending_stock_splits(con, {"database_path": database}):
        raise ValueError("存在未核对拆股，请先核对 Schwab 股数并确认拆股。")
    from market_prices import resolve
    price_csv = resolve(price_csv, "raw", required=True)
    market = latest_market_prices(price_csv, expected_as_of)
    updated: list[str] = []
    retained: list[str] = []
    unchanged: list[str] = []
    _daily_backup_before_mutation(con)
    with con:
        for symbol in SYMBOLS:
            current = con.execute(
                "SELECT as_of,price,source FROM quotes WHERE symbol=?", (symbol,)
            ).fetchone()
            if current is not None and current["as_of"] > market["as_of"]:
                retained.append(symbol)
                continue
            market_price = decimal_text(market["prices"][symbol])
            if (current is not None and current["as_of"] == market["as_of"]
                    and D(current["price"]) == market["prices"][symbol]
                    and current["source"] == "market"):
                unchanged.append(symbol)
                continue
            con.execute(
                "INSERT INTO quote_history(symbol,as_of,price,source,replaced_as_of,replaced_price) "
                "VALUES (?,?,?,'market',?,?)",
                (symbol, market["as_of"], market_price,
                 current["as_of"] if current else None,
                 current["price"] if current else None),
            )
            con.execute(
                "INSERT INTO quotes(symbol,as_of,price,source) VALUES (?,?,?,'market') "
                "ON CONFLICT(symbol) DO UPDATE SET as_of=excluded.as_of, "
                "price=excluded.price, source=excluded.source",
                (symbol, market["as_of"], market_price),
            )
            updated.append(symbol)
    return {"as_of": market["as_of"], "prices": market["prices"],
            "updated": updated, "retained": retained, "unchanged": unchanged}


def set_manual_quote(con: sqlite3.Connection, symbol: str, quote_date: str,
                     price: object) -> dict:
    """Store one manual estimate quote without allowing its date to move backwards."""
    symbol = str(symbol or "").upper()
    quote_date = parse_day(str(quote_date or ""))
    price_value = D(price)
    if symbol not in SYMBOLS or price_value <= 0:
        raise ValueError("请选择 VOO/QQQM 并输入大于零的参考价。")
    current = con.execute(
        "SELECT as_of,price,source FROM quotes WHERE symbol=?", (symbol,)
    ).fetchone()
    if current is not None and current["as_of"] > quote_date:
        raise ValueError(
            f"现有 {symbol} 参考价日期为 {current['as_of']}；"
            "为避免估值倒退，不能用更早日期覆盖。"
        )
    if (current is not None and current["as_of"] == quote_date
            and D(current["price"]) == price_value and current["source"] == "manual"):
        return {"symbol": symbol, "as_of": quote_date, "price": price_value,
                "updated": False}
    _daily_backup_before_mutation(con)
    with con:
        con.execute(
            "INSERT INTO quote_history(symbol,as_of,price,source,replaced_as_of,replaced_price) "
            "VALUES (?,?,?,'manual',?,?)",
            (symbol, quote_date, decimal_text(price_value),
             current["as_of"] if current else None,
             current["price"] if current else None),
        )
        con.execute(
            "INSERT INTO quotes(symbol,as_of,price,source) VALUES (?,?,?,'manual') "
            "ON CONFLICT(symbol) DO UPDATE SET as_of=excluded.as_of, "
            "price=excluded.price, source=excluded.source",
            (symbol, quote_date, decimal_text(price_value)),
        )
    return {"symbol": symbol, "as_of": quote_date, "price": price_value,
            "updated": True}


def validate_cash_history(transactions: list[sqlite3.Row]) -> None:
    balances = {"fixed": Decimal(0), "extra": Decimal(0), "legacy": Decimal(0)}
    source_shares = {(source, symbol): Decimal(0) for source in balances for symbol in SYMBOLS}
    total = Decimal(0)
    splits=sorted(getattr(transactions,'splits',[]),key=lambda r:r['date']);split_index=0
    for r in transactions:
        while split_index<len(splits) and splits[split_index]['date']<=r['date']:
            event=splits[split_index]
            for key in list(source_shares):
                if key[1]==event['symbol']:source_shares[key]*=D(event['ratio'])
            split_index+=1
        source = r["source_type"] or "legacy"
        if r["kind"] == "buy":
            source_shares[(source, r["symbol"])] += D(r["shares"])
        elif r["kind"] == "sell":
            key = (source, r["symbol"])
            source_shares[key] -= D(r["shares"])
            if source_shares[key] < -Decimal("0.000000000001"):
                raise ValueError(f"{r['date']} #{r['id']} 卖出股数超过该资金来源持仓；请核对来源分类。")
        amount, fee = D(r["amount"]), D(r["fee"])
        delta = amount - fee if r["kind"] in ("deposit", "sell", "dividend") else -amount - fee
        balances[source] += delta
        total += delta
        if r["kind"] in ("buy", "withdrawal") and (total < 0 or balances[source] < 0):
            action = "买入" if r["kind"] == "buy" else "出金"
            raise ValueError(f"{r['date']} #{r['id']} {action}时账本现金不足，或对应资金池现金不足；请检查日期、金额或资金来源。")


def _protect_linked_opportunity_buy(con: sqlite3.Connection, transaction_id: int,
                                    operation: str) -> None:
    linked = con.execute(
        "SELECT a.id,a.status FROM opportunity_stage_actions a "
        "JOIN opportunity_action_transactions l ON l.action_id=a.id "
        "WHERE l.transaction_id=?",
        (int(transaction_id),),
    ).fetchone()
    if linked is None:
        return
    if linked["status"] == "executed":
        raise ValueError(
            f"流水 #{transaction_id} 已归入并完成机会档位 #{linked['id']}，不可{operation}。"
        )
    raise ValueError(
        f"流水 #{transaction_id} 已关联机会档位 #{linked['id']}；请先解除关联再{operation}。"
    )


def add_or_edit(
    con: sqlite3.Connection,
    record: dict,
    record_id: int | None = None,
    *,
    va_config: dict | None = None,
) -> int:
    item = validate_record(record)
    fields = (item["date"], item["kind"], item["symbol"], decimal_text(item["amount"]),
              decimal_text(item["price"]) if item["price"] is not None else None,
              decimal_text(item["shares"]) if item["shares"] is not None else None,
              decimal_text(item["fee"]), item["note"], item["source_type"], item["plan_month"],
              item["trigger_note"], item["transfer_date"],
              decimal_text(item["hkd_sent"]) if item["hkd_sent"] is not None else None,
              decimal_text(item["fx_rate_hkd_per_usd"]) if item["fx_rate_hkd_per_usd"] is not None else None,
              decimal_text(item["fx_cost_hkd"]) if item["fx_cost_hkd"] is not None else None)
    _daily_backup_before_mutation(con)
    with con:
        before = None
        if record_id is not None:
            record_id = int(record_id)
            _protect_linked_opportunity_buy(con, record_id, "编辑")
            before = con.execute(
                "SELECT * FROM transactions WHERE id=?", (record_id,)
            ).fetchone()
            if before is None:
                raise ValueError("记录 ID 不存在。")
        if (va_config is not None and item["kind"] == "buy"
                and item["source_type"] == "fixed"
                and item["plan_month"] >= va_config["va_plan"]["start_month"]):
            if item["plan_month"] != item["date"][:7]:
                raise ValueError("VA 买入必须归属实际成交所在月份；跨月未完成额由下月预算重新计算。")
            locked = con.execute(
                "SELECT COUNT(*) FROM va_month_checkpoints WHERE plan_month=?",
                (item["plan_month"],),
            ).fetchone()[0]
            if locked != len(SYMBOLS):
                raise ValueError(
                    f"{item['plan_month']} 的预算尚未锁定；请先到“投资日历”锁定本月预算，再记录买入。"
                )
        if record_id is None:
            cursor = con.execute("INSERT INTO transactions(date,kind,symbol,amount,price,shares,fee,note,source_type,plan_month,trigger_note,transfer_date,hkd_sent,fx_rate_hkd_per_usd,fx_cost_hkd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", fields)
            saved_id = int(cursor.lastrowid)
            after = con.execute("SELECT * FROM transactions WHERE id=?", (saved_id,)).fetchone()
            _record_revision(con, saved_id, "create", None, after)
        else:
            cur = con.execute("UPDATE transactions SET date=?,kind=?,symbol=?,amount=?,price=?,shares=?,fee=?,note=?,source_type=?,plan_month=?,trigger_note=?,transfer_date=?,hkd_sent=?,fx_rate_hkd_per_usd=?,fx_cost_hkd=? WHERE id=?", fields + (record_id,))
            if cur.rowcount == 0:
                raise ValueError("记录 ID 不存在。")
            saved_id = record_id
            after = con.execute("SELECT * FROM transactions WHERE id=?", (saved_id,)).fetchone()
            _record_revision(con, saved_id, "update", before, after)
        calculate(rows(con), get_quotes(con))
        validate_cash_history(rows(con))
        _validate_opportunity_transaction_references(con)
    return saved_id


def add_records_batch(
    con: sqlite3.Connection,
    records: list[dict],
    *,
    va_config: dict | None = None,
) -> dict:
    """Save a quick-entry deposit and its purchases together or not at all."""
    if not records:
        raise ValueError("没有可保存的记录。")
    items = [validate_record(record) for record in records]
    _daily_backup_before_mutation(con)
    with con:
        if va_config is not None:
            for item in items:
                if (item["kind"] != "buy" or item["source_type"] != "fixed"
                        or item["plan_month"] < va_config["va_plan"]["start_month"]):
                    continue
                if item["plan_month"] != item["date"][:7]:
                    raise ValueError(
                        "VA 买入必须归属实际成交所在月份；跨月未完成额由下月预算重新计算。"
                    )
                locked = con.execute(
                    "SELECT COUNT(*) FROM va_month_checkpoints WHERE plan_month=?",
                    (item["plan_month"],),
                ).fetchone()[0]
                if locked != len(SYMBOLS):
                    raise ValueError(
                        f"{item['plan_month']} 的预算尚未锁定；请先到“投资日历”锁定本月预算，再记录买入。"
                    )
        new_ids = set()
        for item in items:
            cursor = con.execute(
                "INSERT INTO transactions(date,kind,symbol,amount,price,shares,fee,note,source_type,plan_month,trigger_note,transfer_date,hkd_sent,fx_rate_hkd_per_usd,fx_cost_hkd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item["date"], item["kind"], item["symbol"], decimal_text(item["amount"]),
                 decimal_text(item["price"]) if item["price"] is not None else None,
                 decimal_text(item["shares"]) if item["shares"] is not None else None,
                 decimal_text(item["fee"]), item["note"], item["source_type"], item["plan_month"],
                 item["trigger_note"], item["transfer_date"],
                 decimal_text(item["hkd_sent"]) if item["hkd_sent"] is not None else None,
                 decimal_text(item["fx_rate_hkd_per_usd"]) if item["fx_rate_hkd_per_usd"] is not None else None,
                 decimal_text(item["fx_cost_hkd"]) if item["fx_cost_hkd"] is not None else None),
            )
            saved_id = int(cursor.lastrowid)
            new_ids.add(saved_id)
            after = con.execute("SELECT * FROM transactions WHERE id=?", (saved_id,)).fetchone()
            _record_revision(con, saved_id, "batch_create", None, after)
        transactions = rows(con)
        state = calculate(transactions, get_quotes(con))
        validate_cash_history(transactions)
        _validate_opportunity_transaction_references(con)
        running_cash = Decimal(0)
        for row in transactions:
            amount, fee = D(row["amount"]), D(row["fee"])
            if row["kind"] in ("deposit", "sell", "dividend"):
                running_cash += amount - fee
            else:
                running_cash -= amount + fee
            if row["id"] in new_ids and row["kind"] == "buy" and running_cash < 0:
                raise ValueError("买入当时账本现金不足；请先补录已到账的美元入金，或核对买入日期与金额。")
        if any(item["kind"] == "buy" for item in items) and state["cash"] < 0:
            raise ValueError("买入后账本现金不足；请先补录已到账的美元入金，或核对买入金额。")
    return state


def delete_record(con: sqlite3.Connection, record_id: int) -> None:
    _daily_backup_before_mutation(con)
    with con:
        record_id = int(record_id)
        _protect_linked_opportunity_buy(con, record_id, "删除")
        before = con.execute("SELECT * FROM transactions WHERE id=?", (record_id,)).fetchone()
        if before is None:
            raise ValueError("记录 ID 不存在。")
        cur = con.execute("DELETE FROM transactions WHERE id=?", (record_id,))
        if cur.rowcount == 0:
            raise ValueError("记录 ID 不存在。")
        _record_revision(con, record_id, "delete", before, None)
        calculate(rows(con), get_quotes(con))
        validate_cash_history(rows(con))
        _validate_opportunity_transaction_references(con)


def ask(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    result = input(f"{label}{suffix}：").strip()
    return result if result else default


def prompt_record(existing: sqlite3.Row | None = None, kind_override: str | None = None) -> dict:
    old = dict(existing) if existing else {}
    kind_map = {"1": "deposit", "2": "withdrawal", "3": "buy", "4": "sell", "5": "dividend"}
    if kind_override:
        kind = kind_override
    else:
        print("类型：1 入金  2 出金  3 买入  4 卖出  5 分红")
        default_kind = old.get("kind", "buy")
        kind_input = ask("类型", default_kind)
        kind = kind_map.get(kind_input, kind_input.lower())
    if kind not in KINDS:
        raise ValueError("未知记录类型。")
    trade = kind in ("buy", "sell")
    symbol = ask("标的 VOO/QQQM", old.get("symbol") or "VOO") if trade or kind == "dividend" else None
    # New records always belong to the single active VA ledger.  When an old
    # historical row is edited, preserve its legacy classification verbatim.
    source = old.get("source_type") or "fixed"
    trade_day = ask("日期 YYYY-MM-DD", old.get("date") or date.today().isoformat())
    plan_month_needed = source == "fixed" and kind in ("deposit", "buy")
    plan_month = (ask(
        "归属 VA 月份 YYYY-MM",
        old.get("plan_month") or trade_day[:7],
    ) if plan_month_needed else old.get("plan_month"))
    record = {
        "date": trade_day,
        "kind": kind,
        "symbol": symbol,
        "amount": ask("金额 USD（买卖为不含交易费用成交额；分红为税前金额）", str(old.get("amount") or "")),
        "price": ask("成交价 USD/股", str(old.get("price") or "")) if trade else None,
        "shares": ask("成交股数", str(old.get("shares") or "")) if trade else None,
        "fee": ask("预扣税及其他费用 USD" if kind == "dividend" else "相关费用 USD", str(old.get("fee") or "0")),
        "note": ask("备注", old.get("note") or ""),
        "source_type": source, "plan_month": plan_month,
        "trigger_note": old.get("trigger_note") or "" if source == "extra" else "",
    }
    if kind == "deposit":
        record["transfer_date"] = ask("港币转出日期（可留空）", old.get("transfer_date") or "")
        record["hkd_sent"] = ask("转出港币 HKD（可留空）", old.get("hkd_sent") or "")
        record["fx_rate_hkd_per_usd"] = ask("实际 HKD/USD 汇率（留空按汇出/到账推算）", old.get("fx_rate_hkd_per_usd") or "")
        record["fx_cost_hkd"] = ask("换汇/汇款成本 HKD（可留空）", old.get("fx_cost_hkd") or "")
    return record


def quick_entry(con: sqlite3.Connection, config: dict) -> bool:
    """Record one VA funding batch and its completed purchases together."""
    state = calculate(rows(con), get_quotes(con))
    print("\n快捷记账｜独立 VA｜嘉信美元账户")
    source = "fixed"
    source_label = "VA 计划"
    plan_month = ask("归属 VA 月份 YYYY-MM", date.today().strftime("%Y-%m"))
    source_cash = plan_and_pool(rows(con), config)["pool"][source]
    print(f"账本现金 ${fmt(state['cash'])}；{source_label}可用现金 ${fmt(source_cash)}。")
    print("仅记录 Schwab 已到账美元及已成交买入；直接回车可跳过入金或买入。")
    records = []
    deposit_raw = ask("本次实际到账的美元入金（无新入金请回车）")
    if deposit_raw:
        deposit = D(deposit_raw)
        if deposit <= 0:
            raise ValueError("入金金额须大于零。")
        records.append({
            "date": ask("入金到账日期 YYYY-MM-DD", date.today().isoformat()),
            "kind": "deposit", "symbol": None, "amount": deposit,
            "price": None, "shares": None,
            "transfer_date": ask("港币转出日期 YYYY-MM-DD（可留空）"),
            "hkd_sent": ask("转出港币 HKD（可留空）"),
            "fx_rate_hkd_per_usd": ask("实际 HKD/USD 汇率（可留空自动推算）"),
            "fx_cost_hkd": ask("换汇/汇款成本 HKD（可留空）"),
            "fee": ask("嘉信美元现金另扣的入金手续费", "0"),
            "note": ask("入金备注（可留空）"),
            "source_type": source, "plan_month": plan_month,
        })
    while True:
        symbol = ask("买入标的 VOO/QQQM（无买入请回车）").upper()
        if not symbol:
            break
        if symbol not in SYMBOLS:
            raise ValueError("买入标的须为 VOO 或 QQQM。")
        trade_date = ask("成交日期 YYYY-MM-DD", date.today().isoformat())
        trade_time = ask("成交时间 HH:MM（可留空）")
        if trade_time and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", trade_time):
            raise ValueError("成交时间须为 HH:MM，例如 09:30。")
        amount_raw = ask("实际成交金额 USD（可留空）")
        price_raw = ask("实际成交价 USD/股（可留空）")
        shares_raw = ask("实际成交股数（可留空）")
        if sum(bool(value) for value in (amount_raw, price_raw, shares_raw)) < 2:
            raise ValueError("买入至少填写成交金额、成交价、股数中的两项。")
        amount = D(amount_raw) if amount_raw else None
        price = D(price_raw) if price_raw else None
        shares = D(shares_raw) if shares_raw else None
        if any(value <= 0 for value in (amount, price, shares) if value is not None):
            raise ValueError("成交金额、成交价和股数须大于零。")
        if amount is None:
            amount = (price * shares).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        elif price is None:
            price = (amount / shares).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
        elif shares is None:
            shares = (amount / price).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
        fee = ask("交易手续费 USD", "0")
        note = ask("买入备注（可留空）")
        if trade_time:
            note = f"成交时间 {trade_time}" + (f"；{note}" if note else "")
        records.append({
            "date": trade_date, "kind": "buy", "symbol": symbol,
            "amount": amount, "price": price, "shares": shares,
            "fee": fee, "note": note,
            "source_type": source, "plan_month": plan_month,
            "trigger_note": "",
        })
    if not records:
        print("未输入记录，已退出。")
        return False
    items = [validate_record(record) for record in records]
    projected_cash, projected_source = state["cash"], source_cash
    print("\n保存前核对：")
    for item in items:
        change = item["amount"] - item["fee"] if item["kind"] == "deposit" else -item["amount"] - item["fee"]
        projected_cash += change
        projected_source += change
        if item["kind"] == "deposit":
            print(f"  {item['date']} {source_label}入金 ${fmt(item['amount'])}，费用 ${fmt(item['fee'])}")
        else:
            print(f"  {item['date']} {source_label}买入 {item['symbol']} {decimal_text(item['shares'])} 股 × ${decimal_text(item['price'])}，成交 ${fmt(item['amount'])}，费用 ${fmt(item['fee'])}")
    print(f"预计账本现金 ${fmt(projected_cash)}；{source_label}现金 ${fmt(projected_source)}")
    if any(item["kind"] == "buy" for item in items) and (projected_cash < 0 or projected_source < 0):
        raise ValueError("买入后对应资金池现金不足；请核对已到账入金、来源和成交金额。")
    if ask("确认保存？输入 Y，直接回车取消").upper() != "Y":
        print("已取消，账本未改变。")
        return False
    add_records_batch(con, records, va_config=config)
    print("记录已保存。")
    return True


def print_records(transactions: list[sqlite3.Row]) -> None:
    if not transactions:
        print("暂无记录。")
        return
    print("ID   日期         类型  标的   金额 USD      价格         股数         费用/代扣  备注")
    for r in transactions:
        note = r["note"].replace("\n", " ")[:30]
        print(f"{r['id']:<4} {r['date']}  {KIND_ZH[r['kind']]:<2}  {(r['symbol'] or '-'):5} "
              f"{fmt(D(r['amount'])):>10}  {(r['price'] or '-'):>10}  {(r['shares'] or '-'):>10}  "
              f"{fmt(D(r['fee'])):>6}   {note}")


def h(value: object) -> str:
    return html.escape(str(value), quote=True)


def build_report(transactions: list[sqlite3.Row], state: dict, config: dict,
                 reviews: list[sqlite3.Row] | None = None,
                 opportunity: dict | None = None,
                 opportunity_events: list[dict] | None = None,
                 opportunity_actions: list[dict] | None = None,
                 va_execution: dict | None = None) -> str:
    positions, values = state["positions"], state["valuations"]
    metrics = plan_and_pool(transactions, config)
    va = va_summary(state, config)
    execution = va_execution or {
        "month": va["month"], "confirmed": False,
        "budget_day": int(config["budget_day"]),
        "budget_date": va_budget_date(config, va["month"]).isoformat(),
        "status": "awaiting_budget_lock",
        "locked_budget_usd": None, "actual_buy_usd": Decimal(0),
        "remaining_usd": None, "completion_pct": None,
        "live_gap_usd": va["suggested_total_usd"],
        "symbols": {
            symbol: {
                "target_value_usd": va["symbols"][symbol]["target_value_usd"],
                "locked_budget_usd": None, "actual_buy_usd": Decimal(0),
                "remaining_usd": None,
                "live_gap_usd": va["symbols"][symbol]["suggested_buy_usd"],
                "reference_market_value_usd": None,
                "reference_price_date": None, "locked_on": None,
            }
            for symbol in SYMBOLS
        },
    }
    target_weights = va_target_weights(va)
    plan = config.get("opportunity_plan", DEFAULTS["opportunity_plan"])
    opportunity = opportunity or {
        "current_month": date.today().strftime("%Y-%m"),
        "start_month": plan["start_month"], "active": False,
        "monthly_target_usd": D(plan["monthly_usd"]),
        "monthly_actual_usd": Decimal(0), "monthly_remaining_usd": Decimal(0),
        "monthly_completion_pct": Decimal(0),
        "balance_usd": Decimal(0), "deployable_usd": Decimal(0),
        "cap_usd": D(plan["cap_usd"]), "excess_usd": Decimal(0),
        "executed_stage_count": 0,
    }
    total = state["market_value"]
    total_basis = sum((positions[symbol].basis for symbol in SYMBOLS), Decimal(0))
    def pnl_class(value: Decimal | None) -> str:
        return "positive" if value is not None and value > 0 else ("negative" if value is not None and value < 0 else "")
    def signed_points(value: Decimal) -> str:
        return f"{'+' if value > 0 else ''}{value:.2f} 个百分点"
    def card(label: str, value: Decimal, *, pnl: bool = False) -> str:
        css = f" {pnl_class(value)}" if pnl else ""
        return f'<div class="card{css}"><span>{h(label)}</span><b>{h(usd(value))}</b></div>'
    month = date.today().strftime("%Y-%m")
    month_buy = metrics["current_fixed_buy"]
    budget = metrics["current_budget"]
    plan_start = config.get("monthly_plan_start")
    plan_active = plan_start is None or month >= plan_start
    plan_label = f"{month} 买入 / 月预算" if plan_active else f"{month} 买入 / 定投未开始"
    plan_amount = f"${fmt(month_buy)} / ${fmt(budget)}" if plan_active else f"${fmt(month_buy)} / {plan_start} 起每月 ${fmt(budget)}"
    budget_schedule_text = "；".join(
        [f"{config.get('monthly_plan_start') or '计划开始'} 起 {usd(config['monthly_budget_usd'])}/月"]
        + [f"{item['effective_month']} 起 {usd(D(item['budget_usd']))}/月"
           for item in config.get("monthly_budget_schedule", [])]
    )
    opportunity_progress_text = (
        f"{opportunity['current_month']} 已实际到账 {usd(opportunity['monthly_actual_usd'])}"
        f"（{opportunity['monthly_completion_pct']:.1f}%），本月剩余 "
        f"{usd(opportunity['monthly_remaining_usd'])}"
        if opportunity.get("active") else
        f"当前尚未开始，首个计划月份为 {opportunity['start_month']}"
    )
    holding_rows = []
    for symbol in SYMBOLS:
        p, v = positions[symbol], values[symbol]
        allocation = v["value"] / total * 100 if total else Decimal(0)
        target = target_weights[symbol]
        drift = allocation - target
        avg = p.basis / p.shares if p.shares else Decimal(0)
        holding_rows.append(
            f"<tr><td>{h(symbol)}</td><td>{h(fmt(p.shares, 8))}</td>"
            f"<td>{h(usd(p.basis))}</td><td>{h(usd(avg, 4))}</td>"
            f"<td>{h(usd(v['price'], 4))}</td><td>{h(v['date'] or '—')}</td><td>{h(v['source'])}</td>"
            f"<td>{h(usd(v['value']))}</td><td class='{pnl_class(v['unrealized'])}'>{h(usd(v['unrealized']))}</td>"
            f"<td class='{pnl_class(p.realized + p.dividends)}'>{h(usd(p.realized + p.dividends))}</td>"
            f"<td>{allocation:.2f}%</td><td>{target:.2f}%</td><td>{h(signed_points(drift))}</td></tr>")
    holdings_total_row = (
        f"<tr><td>持仓合计</td><td>—</td><td>{h(usd(total_basis))}</td><td>—</td><td>—</td><td>—</td><td>—</td>"
        f"<td>{h(usd(state['market_value']))}</td><td class='{pnl_class(state['unrealized'])}'>{h(usd(state['unrealized']))}</td>"
        f"<td class='{pnl_class(state['realized_total'])}'>{h(usd(state['realized_total']))}</td>"
        f"<td>{'100.00%' if total else '0.00%'}</td><td>100.00%</td><td>0.00 个百分点</td></tr>")
    transaction_rows = []
    for r in reversed(transactions):
        source_label = (
            "持仓现金分红" if r["kind"] == "dividend" else
            {"fixed": "VA 计划", "extra": "历史额外资金"}.get(r["source_type"], "未分类")
        )
        transaction_rows.append("<tr>" + "".join(f"<td>{h(x)}</td>" for x in (
            r["id"], r["date"], KIND_ZH[r["kind"]],
            source_label,
            r["plan_month"] or "—", r["symbol"] or "—", fmt(D(r["amount"])),
            r["price"] or "—", r["shares"] or "—", fmt(D(r["fee"])),
            r["trigger_note"] or "—", r["note"] or "—")) + "</tr>")
    dividend_rows = []
    for r in reversed(transactions):
        if r["kind"] != "dividend":
            continue
        gross, tax = D(r["amount"]), D(r["fee"])
        dividend_rows.append("<tr>" + "".join(f"<td>{h(x)}</td>" for x in (
            r["date"], r["symbol"], usd(gross), usd(tax), usd(gross - tax),
            r["note"] or "—")) + "</tr>")
    dividend_symbol_rows = []
    for symbol in SYMBOLS:
        position = positions[symbol]
        dividend_symbol_rows.append("<tr>" + "".join(f"<td>{h(x)}</td>" for x in (
            symbol, position.dividend_count, usd(position.dividend_gross),
            usd(position.dividend_tax), usd(position.dividends),
            position.last_dividend_date or "—")) + "</tr>")
    funding_rows = []
    for r in reversed(transactions):
        if r["kind"] != "deposit":
            continue
        usage = metrics["deposit_usage"][r["id"]]
        if usage["invested"] >= usage["amount"]:
            status = "已用于投资"
        elif usage["invested"] > 0:
            status = "部分已用于投资" + ("/部分出金" if usage["withdrawn"] > 0 else "")
        elif usage["withdrawn"] > 0:
            status = "已出金或部分出金"
        else:
            status = "尚未使用"
        funding_rows.append("<tr>" + "".join(f"<td>{h(x)}</td>" for x in (
            r["date"], r["transfer_date"] or "—", r["hkd_sent"] or "—",
            fmt(D(r["amount"])), r["fx_rate_hkd_per_usd"] or "—", r["fx_cost_hkd"] or "—",
            {"fixed": "VA 计划", "extra": "历史额外资金"}.get(r["source_type"], "未分类"), status,
            fmt(usage["remaining"]))) + "</tr>")
    held_source_keys = {values[s]["source_key"] for s in SYMBOLS if positions[s].shares > 0}
    if not held_source_keys:
        quote_note = "当前没有持仓。"
    elif held_source_keys == {"market"}:
        quote_note = "全部在持标的使用最近一次有效的 Yahoo Finance 实盘收盘价。"
    elif held_source_keys == {"manual"}:
        quote_note = "全部在持标的使用手动参考报价。"
    elif "trade" in held_source_keys:
        quote_note = "部分或全部持仓仍使用最近成交价；请按需更新行情或手动参考报价。"
    else:
        quote_note = "各标的按持仓表所示的最新可用估算价格计算。"
    held_dates = [values[s]["date"] for s in SYMBOLS if positions[s].shares > 0 and values[s]["date"]]
    if held_dates:
        oldest_age = max((date.today() - date.fromisoformat(day)).days for day in held_dates)
        if oldest_age > 7:
            quote_note += f" 最早估算用价格距今天已 {oldest_age} 天。"
    cash_warning = "<p class='warn'>现金余额为负：请补录入金、手续费或检查成交金额。</p>" if state["cash"] < 0 else ""
    rules = config.get("buy_rules", DEFAULTS["buy_rules"])
    rule_labels = (("min_52w_drawdown_pct", "52周高点回撤下限"),
                   ("ma20_position", "20日均线位置"), ("ma50_position", "50日均线位置"),
                   ("ma200_position", "200日均线位置"), ("min_vix", "VIX 下限"),
                   ("min_10y_yield_pct", "10Y 收益率下限"),
                   ("max_10y_yield_pct", "10Y 收益率上限"))
    rule_display = "；".join(f"{label}：{h(rules.get(key) if rules.get(key) not in (None, 'any') else '未启用')}"
                            for key, label in rule_labels)
    review_rows = []
    for review in reviews or []:
        observation = json.loads(review["observation_json"])
        outcome = json.loads(review["evaluation_json"])
        review_rows.append("<tr>" + "".join(f"<td>{h(value)}</td>" for value in (
            review["date"], review["symbol"], outcome["status"],
            observation.get("valuation_note") or "—")) + "</tr>")
    risk_labels = {"pending": "待复核", "full": "全额", "half": "减半", "pause": "暂停"}
    action_status_labels = {"draft": "草稿", "approved": "已批准", "executed": "已执行", "cancelled": "已取消"}
    stage_action_rows = []
    for action in opportunity_actions or []:
        linked_amounts = action.get("linked_amounts_usd") or {}
        actual_split = (
            f"VOO {usd(D(linked_amounts.get('VOO', 0)))} / "
            f"QQQM {usd(D(linked_amounts.get('QQQM', 0)))}"
        )
        actual_total = D(action.get("linked_total_usd") or 0)
        linked_fees = D(action.get("linked_fees_usd") or 0)
        stage_action_rows.append("<tr>" + "".join(f"<td>{h(value)}</td>" for value in (
            action["market_date"], f"第 {action['stage']} 档", action["trigger_symbol"],
            f"{D(action['drawdown_pct']):.2f}%", f"{D(action['rule_ratio_pct']):.0f}%",
            usd(D(action["pool_usd_before"])), usd(D(action["rule_amount_usd"])),
            usd(D(action["effective_amount_usd"])),
            usd(D(action["approved_amount_usd"])) if action.get("approved_amount_usd") else "—",
            actual_split, usd(actual_total), usd(linked_fees),
            risk_labels.get(action["risk_status"], action["risk_status"]),
            action_status_labels.get(action["status"], action["status"]),
            action.get("executed_on") or "—",
            ", ".join(str(value) for value in action.get("transaction_ids", [])) or "—",
            action.get("note") or "—",
        )) + "</tr>")
    funding_cards = "".join((
        card("累计入金", state["deposits"]), card("累计出金", state["withdrawals"]),
        card("净入金", state["net_contributed"]), card("可用现金", state["cash"]),
        card("市场累计投入", state["buys"]), card("当前持仓成本", total_basis),
        card("当前持仓参考市值", state["market_value"]), card("参考账户总资产", state["account_value"]),
    ))
    performance_cards = "".join((
        card("累计已实现盈亏（含分红）", state["realized_total"], pnl=True),
        card("未实现盈亏", state["unrealized"], pnl=True),
        card("累计总盈亏", state["total_pnl"], pnl=True),
        f'<div class="card"><span>{"本月剩余预算" if execution["confirmed"] else "本月预估缺口（未锁定）"}</span>'
        f'<b>{h(usd(execution["remaining_usd"] if execution["confirmed"] else execution["live_gap_usd"]))}</b></div>',
    ))
    dividend_cards = "".join((
        card("累计税前分红", state["dividend_gross"]),
        card("累计预扣税及费用", state["dividend_tax"]),
        card("累计净分红", state["dividends"], pnl=True),
        f'<div class="card"><span>分红入账笔数</span><b>{state["dividend_count"]}</b></div>',
    ))
    execution_rows = []
    for symbol in SYMBOLS:
        item = execution["symbols"][symbol]
        execution_rows.append("<tr>" + "".join(f"<td>{h(value)}</td>" for value in (
            symbol,
            usd(item["target_value_usd"]),
            usd(item["reference_market_value_usd"]) if execution["confirmed"] else "尚未锁定",
            item.get("reference_price_date") or "—",
            usd(item["locked_budget_usd"]) if execution["confirmed"] else "尚未锁定",
            usd(item["actual_buy_usd"]),
            usd(item["remaining_usd"]) if execution["confirmed"] else "尚未锁定",
            f"{item['completion_pct']:.1f}%" if item.get("completion_pct") is not None else "—",
        )) + "</tr>")
    budget_status_text = {
        "waiting_budget_day": "等待预算日",
        "awaiting_budget_lock": "待锁定预算",
        "ready_to_execute": "预算已锁定，待月内分批",
        "partial": "月内分批执行中",
        "completed": "本月已完成",
        "no_buy_needed": "本月无需买入",
        "over_budget": "实际买入超过锁定预算",
        "incomplete": "月末未完成",
        "future": "计划尚未开始",
    }.get(execution["status"], execution["status"])
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VOO / QQQM 真实投资报告</title><style>
:root{{--bg:#f4f7fb;--ink:#17243a;--line:#dce5f0;--blue:#215cad}}*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}}
main{{max-width:1180px;margin:auto;padding:24px}}header{{background:#17345f;color:#fff;padding:24px;border-radius:16px}}
h1{{margin:0;font-size:27px}}header p{{margin:6px 0 0}}section{{background:#fff;padding:20px;margin:16px 0;border:1px solid var(--line);border-radius:14px}}
h2{{margin:0 0 12px;font-size:19px}}h3{{margin:18px 0 9px;font-size:15px}}.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}
.card{{border:1px solid var(--line);border-radius:10px;padding:12px}}.card b{{display:block;font-size:21px}}.card span,.muted{{color:#637389}}
.scroll{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;min-width:820px;font-size:13px}}td,th{{border-bottom:1px solid var(--line);padding:9px;text-align:right;white-space:nowrap}}
td:first-child,th:first-child{{text-align:left}}th{{background:#eef4fc}}tfoot td{{font-weight:700;background:#f0f6f2;border-top:2px solid #cedfd4}}.positive{{color:#287149}}.negative{{color:#ad4541}}.warn{{background:#fff3db;padding:10px;border-radius:8px}}
@media(max-width:700px){{main{{padding:9px}}.grid{{grid-template-columns:repeat(2,1fr)}}header,section{{padding:14px}}}}
</style></head><body><main><header><h1>VOO / QQQM 真实投资档案</h1><p>本地生成：{h(datetime.now().strftime('%Y-%m-%d %H:%M'))} · 记账币种 USD · 真实流水与历史回测分开</p></header>
<section><h2>账户总览</h2>
<h3>资金与持仓</h3><div class="grid">{funding_cards}</div>
<h3>账户与收益</h3><div class="grid">{performance_cards}</div>
<p><strong>当前投资模式：独立 VA（Value Averaging）。</strong> {h(va['start_month'])} 起，VOO 首月目标 {usd(va['initial_targets_usd']['VOO'])}、QQQM 首月目标 {usd(va['initial_targets_usd']['QQQM'])}；之后每月目标分别增加 {usd(va['monthly_steps_usd']['VOO'])} / {usd(va['monthly_steps_usd']['QQQM'])}。两只 ETF 分开计算价值缺口，只买不卖。</p>
<p class="muted">累计已实现盈亏 {usd(state['realized_total'])}＝卖出盈亏 {usd(state['realized_sales'])}＋净分红 {usd(state['dividends'])}。累计净分红＝税前分红 {usd(state['dividend_gross'])}－预扣税及其他费用 {usd(state['dividend_tax'])}。累计总盈亏 {usd(state['total_pnl'])}＝累计已实现盈亏＋未实现盈亏－入金/出金美元费用 {usd(state['funding_fees'])}，也等于参考账户总资产减净入金。以上为 USD 账本口径，不含港币汇率变化及港币费用。现金按流水计算；{h(quote_note)}</p>{cash_warning}</section>
<section><h2>{h(execution['month'])} 月度 VA 执行</h2>
<p><strong>{h(budget_status_text)}</strong> · 预算确定日 {h(execution['budget_date'])}。每月 {config['budget_day']} 日按此前最近完整美股收盘价计算并锁定 VOO、QQQM 各自缺口，随后可在当月分多笔买入。</p>
<div class="scroll"><table><thead><tr><th>标的</th><th>累计目标价值</th><th>预算日参考市值</th><th>行情基准日</th><th>锁定预算</th><th>本月实际买入</th><th>剩余预算</th><th>完成度</th></tr></thead><tbody>{''.join(execution_rows)}</tbody></table></div>
<p class="muted">预算锁定后不会因月内行情更新而改写；买入金额不含手续费并逐笔冲减对应标的预算。未完成金额不会机械叠加到下月，下月会按新的累计目标与当时持仓市值重新计算。当前即时理论缺口 {usd(execution['live_gap_usd'])} 仅供观察。</p></section>
<section><h2>分红入账</h2><div class="grid">{dividend_cards}</div>
<h3>按标的汇总</h3><div class="scroll"><table><thead><tr><th>标的</th><th>笔数</th><th>税前分红</th><th>预扣税及费用</th><th>净分红</th><th>最近入账日</th></tr></thead><tbody>{''.join(dividend_symbol_rows)}</tbody></table></div>
<h3>分红明细</h3><div class="scroll"><table><thead><tr><th>入账日</th><th>标的</th><th>税前分红</th><th>预扣税及费用</th><th>净到账</th><th>备注</th></tr></thead><tbody>{''.join(dividend_rows) or '<tr><td colspan="6">尚无分红记录</td></tr>'}</tbody></table></div>
<p class="muted">分红按 Schwab 实际入账日记录。净到账增加账户现金并计入已实现盈亏，但不计入累计入金、市场累计投入或 VA 月度买入完成度。自动再投资时应把分红和买入分别记录。</p></section>
<section><h2>持仓与配置</h2><p class="muted">当月 VA 目标价值结构：VOO {target_weights['VOO']:.2f}% / QQQM {target_weights['QQQM']:.2f}%。实际比例按参考市值计算，不含现金；该结构只供观察，不触发卖出再平衡。</p>
<div class="scroll"><table><thead><tr><th>标的</th><th>股数</th><th>当前持仓成本</th><th>平均成本/股</th><th>估算用价格</th><th>价格日期</th><th>价格来源</th><th>当前持仓参考市值</th><th>未实现盈亏</th><th>累计已实现盈亏</th><th>实际占比</th><th>目标占比</th><th>偏离目标</th></tr></thead><tbody>{''.join(holding_rows)}</tbody><tfoot>{holdings_total_row}</tfoot></table></div>
<p class="muted">偏离目标＝实际占比－目标占比，仅用于后续定投分配参考。卖出使用移动平均成本估算；累计已实现盈亏包括扣除交易费用后的卖出盈亏及净分红，可能与 Schwab 税务批次结果不同。估算用价格不是实时行情。</p></section>
<section><h2>入金与使用</h2><div class="scroll"><table><thead><tr><th>到账日</th><th>港币汇出日</th><th>汇出 HKD</th><th>到账 USD</th><th>HKD/USD</th><th>成本 HKD</th><th>来源</th><th>使用状态</th><th>尚未分配 USD</th></tr></thead><tbody>{''.join(funding_rows) or '<tr><td colspan="9">尚无入金</td></tr>'}</tbody></table></div><p class="muted">使用状态按同类资金入金先后与买入/出金自动分配；卖出所得单独回到资金池。港币成本仅作记录，不从美元现金重复扣除。</p></section>
<section><h2>VA 规则</h2><p>VOO 与 QQQM 完全分开计算：每只 ETF 都有自己的目标价值路径。预算日参考市值低于当月目标时，差额成为该标的本月锁定预算；高于或等于目标时本月无需买入。两只 ETF 之间不合并缺口、不按比例缩放。</p><p class="muted">这是只买不卖的目标价值补足法。每月先确定预算，再在月内择机分批；持仓成本只用于收益核算和展示，最终成交仍以实际可用现金和 Schwab 下单结果为准。</p></section>
<section><h2>逐笔流水</h2><div class="scroll"><table><thead><tr><th>ID</th><th>日期</th><th>类型</th><th>来源</th><th>计划月份</th><th>标的</th><th>金额</th><th>价格</th><th>股数</th><th>费用 / 代扣</th><th>触发条件</th><th>备注</th></tr></thead><tbody>{''.join(transaction_rows) or '<tr><td colspan="12">尚无流水</td></tr>'}</tbody></table></div></section>
<section><h2>口径</h2><p>本报告来自本地 SQLite 流水；Yahoo Finance 自动复权日收盘价、手动参考报价或最近成交价只供估值和复盘。分红金额填写税前金额，预扣税及其他费用填入费用栏，净分红按两者之差计算。汇款、换汇、税务及券商账户中未录入的活动不会自动出现。历史回测是独立模拟，不进入此账户。</p></section></main></body></html>"""


def yaml_value(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def md_cell(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>").replace("\r", "")


def sync_obsidian(transactions: list[sqlite3.Row], state: dict, config: dict,
                  opportunity: dict | None = None,
                  va_execution: dict | None = None) -> Path | None:
    if not config["obsidian_sync_enabled"]:
        return None
    folder = config["obsidian_vault_path"] / config["obsidian_subdir"]
    notes_dir = folder / "流水"
    notes_dir.mkdir(parents=True, exist_ok=True)
    desired = set()
    for r in transactions:
        name = f"{r['date']}-{r['id']:06d}.md"
        desired.add(name)
        source_label = (
            "持仓现金分红" if r["kind"] == "dividend" else
            {"fixed": "VA 计划", "extra": "历史额外资金"}.get(r["source_type"], "未分类")
        )
        amount_label = "税前分红" if r["kind"] == "dividend" else "金额"
        fee_label = "预扣税及其他费用" if r["kind"] == "dividend" else "相关费用"
        net_line = (
            f"- 净到账：${decimal_text(D(r['amount']) - D(r['fee']))}\n"
            if r["kind"] == "dividend" else ""
        )
        content = ("---\n" +
                   "generator: voo_qqqm_investment_tool\n" +
                   "type: investment_transaction\n" +
                   f"transaction_id: {r['id']}\n" +
                   f"date: {r['date']}\n" +
                   f"kind: {r['kind']}\n" +
                   f"source_type: {r['source_type'] or 'null'}\n" +
                   f"plan_month: {r['plan_month'] or 'null'}\n" +
                   f"symbol: {yaml_value(r['symbol'] or '')}\n" +
                   f"amount_usd: {r['amount']}\n" +
                   f"price_usd: {r['price'] or 'null'}\n" +
                   f"shares: {r['shares'] or 'null'}\n" +
                   f"fee_usd: {r['fee']}\n" +
                   f"hkd_sent: {r['hkd_sent'] or 'null'}\n" +
                   f"fx_rate_hkd_per_usd: {r['fx_rate_hkd_per_usd'] or 'null'}\n" +
                   f"trigger_note: {yaml_value(r['trigger_note'])}\n" +
                   f"note: {yaml_value(r['note'])}\n" +
                   "tags: [投资, 美股]\n---\n\n" +
                   f"# {r['date']} {KIND_ZH[r['kind']]} {r['symbol'] or ''}\n\n" +
                   f"- {amount_label}：${r['amount']}\n- {fee_label}：${r['fee']}\n" +
                   net_line +
                   (f"- 成交价：${r['price']} / 股\n- 股数：{r['shares']}\n" if r['price'] else "") +
                   f"- 资金来源：{source_label}\n" +
                   f"- 计划月份：{r['plan_month'] or '无'}\n" +
                   f"- 触发理由：{md_cell(r['trigger_note']) or '无'}\n" +
                   f"- 备注：{md_cell(r['note']) or '无'}\n\n> 本页由投资工具生成；请在程序中修改原始记录。\n")
        generated_note(notes_dir / name, content, r["id"])
    for path in notes_dir.iterdir():
        if path.is_file() and re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{6}\.md", path.name) and path.name not in desired:
            marker_id = int(path.stem.rsplit("-", 1)[1])
            with path.open(encoding="utf-8") as old_note:
                prefix = old_note.read(300)
            if (prefix.startswith("---\ngenerator: voo_qqqm_investment_tool\n")
                    and f"transaction_id: {marker_id}\n" in prefix):
                path.unlink()
    lines = ["---", "generator: voo_qqqm_investment_tool", "type: investment_transactions", f"as_of: {date.today().isoformat()}", "currency: USD", "tags: [投资, 美股]", "---", "", "# 投资流水", "", "| ID | 日期 | 类型 | 来源 | 计划月份 | 标的 | 金额 USD | 价格 | 股数 | 费用 / 代扣 | 触发理由 | 备注 |", "| ---: | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- |"]
    for r in reversed(transactions):
        link = f"[[流水/{r['date']}-{r['id']:06d}|{r['id']}]]"
        lines.append("| " + " | ".join(md_cell(x) for x in (link, r["date"], KIND_ZH[r["kind"]],
            {"fixed": "VA 计划", "extra": "历史额外资金"}.get(r["source_type"], "未分类"),
            r["plan_month"] or "—", r["symbol"] or "—", r["amount"], r["price"] or "—",
            r["shares"] or "—", r["fee"], r["trigger_note"] or "—", r["note"])) + " |")
    generated_note(folder / "投资流水.md", "\n".join(lines) + "\n")
    metrics = plan_and_pool(transactions, config)
    va = va_summary(state, config)
    execution = va_execution or {
        "month": va["month"], "confirmed": False,
        "budget_day": int(config["budget_day"]),
        "budget_date": va_budget_date(config, va["month"]).isoformat(),
        "locked_on": None, "status": "awaiting_budget_lock",
        "locked_budget_usd": None, "actual_buy_usd": Decimal(0),
        "remaining_usd": None, "completion_pct": None,
        "live_gap_usd": va["suggested_total_usd"],
        "symbols": {
            symbol: {
                "locked_budget_usd": None, "actual_buy_usd": Decimal(0),
                "remaining_usd": None,
                "live_gap_usd": va["symbols"][symbol]["suggested_buy_usd"],
                "reference_market_value_usd": None,
                "reference_price_date": None,
            }
            for symbol in SYMBOLS
        },
    }
    target_weights = va_target_weights(va)
    plan = config.get("opportunity_plan", DEFAULTS["opportunity_plan"])
    opportunity = opportunity or {
        "start_month": plan["start_month"], "active": False,
        "monthly_target_usd": D(plan["monthly_usd"]),
        "monthly_actual_usd": Decimal(0), "monthly_remaining_usd": Decimal(0),
        "monthly_completion_pct": Decimal(0),
        "balance_usd": Decimal(0), "cap_usd": D(plan["cap_usd"]),
        "deployable_usd": Decimal(0), "excess_usd": Decimal(0),
        "executed_stage_count": 0,
    }
    total_basis = sum((state["positions"][symbol].basis for symbol in SYMBOLS), Decimal(0))
    def metadata_money(value: Decimal | None) -> str:
        return f"{value:.2f}" if value is not None else "null"
    metadata = [
             f"as_of: {date.today().isoformat()}",
             "investment_mode: independent_va",
             f"va_start_month: {va['start_month']}",
             f"va_current_month: {va['month']}",
             f"va_voo_target_usd: {va['symbols']['VOO']['target_value_usd']:.2f}",
             f"va_voo_current_market_value_usd: {va['symbols']['VOO']['current_market_value_usd']:.2f}",
             f"va_voo_suggested_buy_usd: {va['symbols']['VOO']['suggested_buy_usd']:.2f}",
             f"va_voo_next_target_usd: {(va['symbols']['VOO']['target_value_usd'] + va['monthly_steps_usd']['VOO']):.2f}",
             f"va_qqqm_target_usd: {va['symbols']['QQQM']['target_value_usd']:.2f}",
             f"va_qqqm_current_market_value_usd: {va['symbols']['QQQM']['current_market_value_usd']:.2f}",
             f"va_qqqm_suggested_buy_usd: {va['symbols']['QQQM']['suggested_buy_usd']:.2f}",
             f"va_qqqm_next_target_usd: {(va['symbols']['QQQM']['target_value_usd'] + va['monthly_steps_usd']['QQQM']):.2f}",
             f"va_cash_required_usd: {va['suggested_total_usd']:.2f}",
             f"budget_day: {config['budget_day']}",
             f"budget_date: {execution['budget_date']}",
             f"budget_locked: {str(bool(execution['confirmed'])).lower()}",
             f"budget_locked_on: {execution['locked_on'] or 'null'}",
             f"va_locked_budget_usd: {metadata_money(execution['locked_budget_usd'])}",
             f"va_actual_buy_usd: {execution['actual_buy_usd']:.2f}",
             f"va_remaining_budget_usd: {metadata_money(execution['remaining_usd'])}",
             f"va_voo_locked_budget_usd: {metadata_money(execution['symbols']['VOO']['locked_budget_usd'])}",
             f"va_voo_actual_buy_usd: {execution['symbols']['VOO']['actual_buy_usd']:.2f}",
             f"va_voo_remaining_budget_usd: {metadata_money(execution['symbols']['VOO']['remaining_usd'])}",
             f"va_qqqm_locked_budget_usd: {metadata_money(execution['symbols']['QQQM']['locked_budget_usd'])}",
             f"va_qqqm_actual_buy_usd: {execution['symbols']['QQQM']['actual_buy_usd']:.2f}",
             f"va_qqqm_remaining_budget_usd: {metadata_money(execution['symbols']['QQQM']['remaining_usd'])}",
             f"target_voo_pct: {decimal_text(target_weights['VOO'])}",
             f"target_qqqm_pct: {decimal_text(target_weights['QQQM'])}",
             f"deposits_usd: {decimal_text(state['deposits'])}", f"withdrawals_usd: {decimal_text(state['withdrawals'])}",
             f"net_contributed_usd: {decimal_text(state['net_contributed'])}", f"historical_buys_usd: {decimal_text(state['buys'])}",
             f"cash_usd: {state['cash']:.2f}", f"current_basis_usd: {total_basis:.2f}",
             f"market_value_usd: {state['market_value']:.2f}", f"account_value_usd: {state['account_value']:.2f}",
             f"realized_sales_pnl_usd: {state['realized_sales']:.2f}",
             f"gross_dividends_usd: {state['dividend_gross']:.2f}",
             f"dividend_tax_and_fees_usd: {state['dividend_tax']:.2f}",
             f"net_dividends_usd: {state['dividends']:.2f}",
             f"unrealized_pnl_usd: {state['unrealized']:.2f}", f"realized_pnl_usd: {state['realized_total']:.2f}",
             f"funding_fees_usd: {state['funding_fees']:.2f}", f"total_pnl_usd: {state['total_pnl']:.2f}"]
    front = [
             f"- 累计入金：{usd(state['deposits'])}；累计出金：{usd(state['withdrawals'])}；净入金：{usd(state['net_contributed'])}",
             f"- 可用现金：{usd(state['cash'])}；参考账户总资产：{usd(state['account_value'])}",
             f"- 市场累计投入：{usd(state['buys'])}；当前持仓成本：{usd(total_basis)}；当前持仓参考市值：{usd(state['market_value'])}",
             f"- 累计已实现盈亏：{usd(state['realized_total'])}（卖出盈亏 {usd(state['realized_sales'])}＋净分红 {usd(state['dividends'])}）",
             f"- 分红：税前 {usd(state['dividend_gross'])}；预扣税及其他费用 {usd(state['dividend_tax'])}；净到账 {usd(state['dividends'])}；共 {state['dividend_count']} 笔。",
             f"- 未实现盈亏：{usd(state['unrealized'])}；累计总盈亏：{usd(state['total_pnl'])}；入金/出金美元费用：{usd(state['funding_fees'])}",
             "",
             f"## {va['month']} 独立 VA 月度执行",
             "",
             f"- 预算确定日：每月 {config['budget_day']} 日；本月预算日 {execution['budget_date']}；锁定状态：{'已锁定' if execution['confirmed'] else '尚未锁定'}；实际锁定日：{execution['locked_on'] or '—'}。",
             f"- VOO：累计目标 {usd(va['symbols']['VOO']['target_value_usd'])}；预算日参考市值 {usd(execution['symbols']['VOO']['reference_market_value_usd']) if execution['confirmed'] else '尚未锁定'}；锁定预算 {usd(execution['symbols']['VOO']['locked_budget_usd']) if execution['confirmed'] else '尚未锁定'}；已买 {usd(execution['symbols']['VOO']['actual_buy_usd'])}；剩余 {usd(execution['symbols']['VOO']['remaining_usd']) if execution['confirmed'] else '尚未锁定'}。",
             f"- QQQM：累计目标 {usd(va['symbols']['QQQM']['target_value_usd'])}；预算日参考市值 {usd(execution['symbols']['QQQM']['reference_market_value_usd']) if execution['confirmed'] else '尚未锁定'}；锁定预算 {usd(execution['symbols']['QQQM']['locked_budget_usd']) if execution['confirmed'] else '尚未锁定'}；已买 {usd(execution['symbols']['QQQM']['actual_buy_usd'])}；剩余 {usd(execution['symbols']['QQQM']['remaining_usd']) if execution['confirmed'] else '尚未锁定'}。",
             f"- 当前即时理论缺口：{usd(execution['live_gap_usd'])}，仅用于观察，不会改写已锁定预算。",
             "- 规则：每月先锁定预算，再在月内择机分批；未完成金额不机械结转，下月按新目标和当时市值重新计算。",
             "", "| 标的 | 股数 | 当前持仓成本 | 平均成本/股 | 估算用价格 | 价格日期 | 当前持仓参考市值 | 未实现盈亏 | 实际占比 | 目标占比 | 偏离目标 |",
             "| --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: |"]
    total = state["market_value"]
    for symbol in SYMBOLS:
        p, v = state["positions"][symbol], state["valuations"][symbol]
        ratio = v["value"] / total * 100 if total else Decimal(0)
        target = target_weights[symbol]
        avg = p.basis / p.shares if p.shares else Decimal(0)
        front.append(f"| {symbol} | {fmt(p.shares, 8)} | {usd(p.basis)} | {usd(avg, 4)} | {usd(v['price'], 4)} | {v['date'] or '—'} | {usd(v['value'])} | {usd(v['unrealized'])} | {ratio:.2f}% | {target:.2f}% | {'+' if ratio > target else ''}{ratio - target:.2f} 个百分点 |")
    front.append(f"| **持仓合计** | — | **{usd(total_basis)}** | — | — | — | **{usd(state['market_value'])}** | **{usd(state['unrealized'])}** | **{'100.00%' if total else '0.00%'}** | **100.00%** | **0.00 个百分点** |")
    front += ["", "[[投资流水|查看逐笔流水]]", "", "> 本页由本地 SQLite 流水生成；估算用价格不是实时行情。盈亏为 USD 账本口径，不含港币汇率变化及港币费用；卖出使用移动平均成本估算，可能与券商税务批次不同。请在程序中修改原始记录。", ""]
    marked_overview(folder / "实盘总览.md", "\n".join(metadata), "\n".join(front))
    return folder


def sync_backtest_summary(report: Path, out: Path, config: dict, *, refreshed: bool = False) -> Path | None:
    """Publish simulated results separately from real transactions."""
    if not config["obsidian_sync_enabled"]:
        return None
    with (out / "source_adjusted_close.csv").open(newline="", encoding="utf-8") as handle:
        prices = csv.DictReader(handle)
        first = next(prices, None)
        last = first
        for row in prices:
            last = row
    if not first or not last or "Date" not in first or "Date" not in last:
        raise ValueError("回测行情缺少日期，未同步 Obsidian 摘要。")
    with (out / "five_portfolios_summary.csv").open(newline="", encoding="utf-8") as handle:
        results = list(csv.DictReader(handle))
    if not results:
        raise ValueError("回测结果为空，未同步 Obsidian 摘要。")
    columns = (
        ("portfolio", "VOO/QQQM"),
        ("contributed_usd", "累计投入 USD"),
        ("ending_usd", "期末资产 USD"),
        ("profit_usd", "模拟盈亏 USD"),
        ("money_weighted_xirr", "XIRR"),
        ("max_twr_drawdown", "最大回撤"),
    )
    if any(key not in row for row in results for key, _ in columns):
        raise ValueError("回测结果缺少摘要字段，未同步 Obsidian 摘要。")
    lines = [
        "---", "generator: voo_qqqm_investment_tool", "type: investment_backtest",
        f"as_of: {last['Date']}",
        f"backtest_monthly_budget_usd: {decimal_text(config['backtest_monthly_budget_usd'])}",
        f"initial_extra_usd: {decimal_text(config['backtest_initial_usd'])}",
        f"backtest_execution_day: {config['backtest_execution_day']}",
        "tags: [投资, 美股, 回测]", "---", "", "# VOO / QQQM 回测摘要", "",
        "> 历史模拟，不是真实持仓或已实现收益。参数是回测假设，不代表实际定投计划。", "",
        f"- 行情范围：{first['Date']} 至 {last['Date']}（{'本次经 Yahoo Finance 联网刷新' if refreshed else '本次使用本地既有行情'}；未逐日独立核验）",
        f"- 首月额外本金：${fmt(config['backtest_initial_usd'])}",
        f"- 历史研究全区间每月固定投入：${fmt(config['backtest_monthly_budget_usd'])}",
        f"- 研究模型：首个共同交易日独立投入额外本金；每月在 {config['backtest_execution_day']} 日当天或之后的首个共同交易日一次性模拟买入。该日期只是可复现的回测假设，不代表真实账户的月内分批时点。可买零碎份额，不卖出再平衡。未计税费、汇兑及跨境成本。",
        "", "| " + " | ".join(label for _, label in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in results:
        values = []
        for key, _ in columns:
            value = row[key]
            if key in ("money_weighted_xirr", "max_twr_drawdown"):
                try:
                    number = Decimal(value)
                    value = f"{number * 100:.2f}%" if number.is_finite() else "—"
                except InvalidOperation:
                    value = "—"
            elif key in ("contributed_usd", "ending_usd", "profit_usd"):
                try:
                    number = Decimal(value)
                    value = fmt(number) if number.is_finite() else "—"
                except InvalidOperation:
                    value = "—"
            values.append(md_cell(value))
        lines.append("| " + " | ".join(values) + " |")
    lines += ["", f"完整 HTML 报告（本机）：`{report}`", "", "> 本页由本地回测生成；不会写入真实交易账本。", ""]
    target = config["obsidian_vault_path"] / config["obsidian_subdir"] / "回测摘要.md"
    generated_note(target, "\n".join(lines))
    return target



def sync_va_backtest_summary(report: Path, out: Path, config: dict, *, refreshed: bool = False) -> Path | None:
    """Sync only Independent VA vs fixed DCA research to Obsidian.

    Mixed VA is intentionally excluded from the Obsidian summary.  The vault is
    the execution/review surface for the user's real independent-VA strategy;
    DCA appears only as the benchmark needed to judge whether VA added value.
    """
    if not config["obsidian_sync_enabled"]:
        return None
    payload_path = out / "va_strategy_summary.json"
    if not payload_path.exists():
        raise ValueError("VA 回测摘要不存在，未同步 Obsidian。")
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    metrics = payload.get("metrics") or {}
    params = payload.get("parameters") or {}
    required = ("voo", "voo_dca", "qqqm", "qqqm_dca", "independent_total", "fixed_dca")
    if any(key not in metrics for key in required):
        raise ValueError("VA 回测结果缺少 Independent VA / DCA 指标，未同步 Obsidian。")

    def pct(value: object) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "—"
        if not math.isfinite(number):
            return "—"
        return f"{number * 100:.2f}%"

    def money(value: object) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "—"
        if not math.isfinite(number):
            return "—"
        return f"${number:,.2f}"

    def metric_line(label: str, va_key: str, dca_key: str) -> list[str]:
        va_m, dca_m = metrics[va_key], metrics[dca_key]
        return [
            f"### {label}", "",
            "| 指标 | 独立 VA | 固定 DCA |",
            "| --- | ---: | ---: |",
            f"| 累计投入 | {money(va_m.get('contributed_usd'))} | {money(dca_m.get('contributed_usd'))} |",
            f"| 期末资产 | {money(va_m.get('ending_usd'))} | {money(dca_m.get('ending_usd'))} |",
            f"| 盈利 | {money(va_m.get('profit_usd'))} | {money(dca_m.get('profit_usd'))} |",
            f"| XIRR | {pct(va_m.get('money_weighted_xirr'))} | {pct(dca_m.get('money_weighted_xirr'))} |",
            f"| TWR 年化 | {pct(va_m.get('time_weighted_cagr'))} | {pct(dca_m.get('time_weighted_cagr'))} |",
            f"| 最大回撤 | {pct(va_m.get('max_twr_drawdown'))} | {pct(dca_m.get('max_twr_drawdown'))} |",
            f"| 投入次数 | {va_m.get('num_contributions', '—')} | {dca_m.get('num_contributions', '—')} |",
            f"| 最大单次投入 | {money(va_m.get('max_contribution_usd'))} | {money(dca_m.get('max_contribution_usd'))} |",
            "",
        ]

    start = metrics["independent_total"].get("start", "—")
    end = metrics["independent_total"].get("end", "—")
    lines = [
        "---", "generator: voo_qqqm_investment_tool", "type: independent_va_backtest",
        f"as_of: {end}", "strategy: independent_va", "benchmark: fixed_dca",
        f"voo_initial_target_usd: {params.get('voo_initial', 'null')}",
        f"qqqm_initial_target_usd: {params.get('qqqm_initial', 'null')}",
        f"voo_monthly_step_usd: {params.get('voo_step', 'null')}",
        f"qqqm_monthly_step_usd: {params.get('qqqm_step', 'null')}",
        f"backtest_execution_day: {params.get('target_day', config.get('backtest_execution_day', 10))}",
        "tags: [投资, 美股, VA, 回测]", "---", "",
        "# 独立 VA vs 固定 DCA 回测摘要", "",
        "> 本页只同步真实执行策略 Independent VA 与固定 DCA 基准。混合 VA 不进入 Obsidian。", "",
        f"- 回测区间：{start} 至 {end}（{'本次先联网刷新行情' if refreshed else '使用本地既有行情'}）",
        f"- VOO：首月目标 {money(params.get('voo_initial'))}，此后每月目标 +{money(params.get('voo_step'))}；DCA 基准每月 {money(params.get('voo_step'))}。",
        f"- QQQM：首月目标 {money(params.get('qqqm_initial'))}，此后每月目标 +{money(params.get('qqqm_step'))}；DCA 基准每月 {money(params.get('qqqm_step'))}。",
        "- Independent VA：VOO、QQQM 各自计算目标缺口，互不抵消，只买不卖。", "",
    ]
    lines += metric_line("VOO", "voo", "voo_dca")
    lines += metric_line("QQQM", "qqqm", "qqqm_dca")
    lines += metric_line("整体", "independent_total", "fixed_dca")
    lines += [
        "> VA 与 DCA 的累计投入金额可能不同，所以不要只比较期末资产；应结合 XIRR、TWR、最大回撤和最大单次资金需求。", "",
        f"完整 HTML 报告（本机）：`{report}`", "",
        "> 本页由历史模拟生成，不写入真实交易账本，也不代表未来收益。", "",
    ]
    target = config["obsidian_vault_path"] / config["obsidian_subdir"] / "回测摘要.md"
    generated_note(target, "\n".join(lines))
    return target


def refresh(con: sqlite3.Connection, config: dict) -> tuple[dict, Path]:
    transactions = rows(con)
    quotes = get_quotes(con)
    state = calculate(transactions, quotes)
    execution = va_execution_summary(
        con, config, transactions=transactions, state=state
    )
    report = config["report_path"]
    report_content = build_report(transactions, state, config, va_execution=execution)
    reference_warnings = va_budget_reference_warnings(con, transactions)
    pending = pending_stock_splits(con, config)
    if pending:
        reference_warnings.append("检测到尚未核对的拆股，请在投资日历核对 Schwab 股数；当前参考市值不宜用于收益判断。")
    if reference_warnings:
        warning_html = "<section><h2>预算基准需复核</h2>" + "".join(
            f"<p class='warn'>{h(message)}</p>" for message in reference_warnings
        ) + "</section>"
        report_content = report_content.replace("<body>", "<body>" + warning_html, 1)
    atomic_text(report, report_content)
    location = sync_obsidian(
        transactions, state, config, va_execution=execution
    )
    print(f"中文投资报告：{report}")
    if location:
        print(f"Obsidian 已同步：{location}")
    return state, report


def refresh_market_data(script_dir: Path) -> dict:
    """Refresh validated inputs used by valuation, the briefing, and VA research."""
    from backtest import refresh_source_prices

    bundled = script_dir / "data" / "source_adjusted_close.csv"
    refresh_info = refresh_source_prices(bundled)
    print(f"已联网核对历史行情：共同交易日截至 {refresh_info['latest_date']}，"
          f"本次目标截止日 {refresh_info['cutoff_date']}。")
    advice_context = None
    advice_warning = None
    try:
        from market_advice import refresh_market_context
        advice_context = refresh_market_context(
            script_dir / "data" / "market_context.json",
            date.fromisoformat(refresh_info["latest_date"]),
        )
        for warning in advice_context.get("warnings", []):
            print(f"市场环境数据提示：{warning}")
    except (ValueError, OSError, UnicodeError) as exc:
        advice_warning = str(exc)
        print(f"晨报所需市场环境数据未更新：{exc}")
    return {"refresh_info": refresh_info, "advice_context": advice_context,
            "advice_warning": advice_warning}


def run_opportunity_backtest_study(config: dict, script_dir: Path,
                                   standard_result: dict) -> tuple[Path | None, str]:
    """Run the separate all-USD opportunity-fund study."""
    from opportunity_backtest import run_opportunity_backtest

    plan = config["opportunity_plan"]
    source_label = (
        "Yahoo Finance 自动复权日收盘价；本轮标准回测已联网刷新"
        if standard_result.get("refreshed") else
        "本地既有 VOO/QQQM 自动复权日收盘价；本轮未联网刷新"
    )
    report = run_opportunity_backtest(
        script_dir / "data" / "source_adjusted_close.csv",
        config["opportunity_backtest_output_dir"],
        monthly_dca_usd=float(config["backtest_monthly_budget_usd"]),
        opportunity_monthly_usd=float(D(plan["monthly_usd"])),
        opportunity_cap_usd=float(D(plan["cap_usd"])),
        opportunity_start_month=plan["start_month"],
        target_day=config["backtest_execution_day"],
        overflow_dca_months=plan["excess_spread_months"],
        voo_weight=float(config["target_weights"]["VOO"] / 100),
        qqqm_weight=float(config["target_weights"]["QQQM"] / 100),
        source_label=source_label,
    )
    cleanup = cleanup_generated_reports(config, protect=(report,))
    cleanup_note = (
        f"；自动回收 {cleanup['removed_count']} 份旧回测报告"
        if cleanup["removed_count"] else ""
    )
    if cleanup["warnings"]:
        cleanup_note += "；生成文件回收提示：" + "；".join(cleanup["warnings"])
    return report, f"机会资金策略回测也已完成（统一 USD 口径）{cleanup_note}"


DATED_REPORT_RULES = (
    ("va_backtest_output_dir", re.compile(r"va_backtest_analysis_(\d{4}-\d{2}-\d{2})\.html")),
)


def generated_report_storage(config: dict) -> dict:
    """Summarize only dated, tool-generated HTML reports eligible for recycling."""
    count = 0
    size_bytes = 0
    by_group: dict[str, int] = {}
    warnings: list[str] = []
    for config_key, pattern in DATED_REPORT_RULES:
        directory = Path(config[config_key])
        group_count = 0
        if directory.is_dir():
            try:
                children = list(directory.iterdir())
            except OSError as exc:
                warnings.append(f"无法读取 {directory.name}：{exc}")
                children = []
            for candidate in children:
                if candidate.is_symlink() or not candidate.is_file():
                    continue
                match = pattern.fullmatch(candidate.name)
                if not match:
                    continue
                try:
                    date.fromisoformat(match.group(1))
                    size_bytes += candidate.stat().st_size
                except (ValueError, OSError):
                    continue
                count += 1
                group_count += 1
        by_group[config_key] = group_count
    return {
        "keep_count": config["generated_report_keep_count"],
        "report_count": count,
        "size_bytes": size_bytes,
        "groups": by_group,
        "warnings": warnings,
    }


def cleanup_generated_reports(config: dict, *, protect: tuple[Path, ...] = ()) -> dict:
    """Keep the newest known dated reports; never touch data, caches, or unknown files."""
    keep_count = config["generated_report_keep_count"]
    protected = {Path(item).expanduser().resolve() for item in protect}
    removed: list[str] = []
    removed_bytes = 0
    warnings: list[str] = []
    for config_key, pattern in DATED_REPORT_RULES:
        directory = Path(config[config_key])
        if not directory.is_dir():
            continue
        candidates: list[tuple[date, int, Path]] = []
        try:
            children = list(directory.iterdir())
        except OSError as exc:
            warnings.append(f"无法读取 {directory.name}：{exc}")
            continue
        for candidate in children:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            match = pattern.fullmatch(candidate.name)
            if not match:
                continue
            try:
                report_day = date.fromisoformat(match.group(1))
                modified_ns = candidate.stat().st_mtime_ns
            except (ValueError, OSError):
                continue
            candidates.append((report_day, modified_ns, candidate))
        candidates.sort(key=lambda item: (item[0], item[1], item[2].name), reverse=True)
        keep = {item[2].resolve() for item in candidates[:keep_count]} | protected
        for _, _, candidate in candidates:
            if candidate.resolve() in keep:
                continue
            try:
                size = candidate.stat().st_size
                candidate.unlink()
            except OSError as exc:
                warnings.append(f"无法回收 {candidate.name}：{exc}")
                continue
            removed.append(candidate.name)
            removed_bytes += size
    return {
        "keep_count": keep_count,
        "removed_count": len(removed),
        "removed_bytes": removed_bytes,
        "removed": removed,
        "warnings": warnings,
        "storage": generated_report_storage(config),
    }


def run_backtest(config: dict, script_dir: Path, *, refresh_prices: bool = True,
                 include_opportunity: bool = False) -> dict:
    """Run the only active strategy study: independent buy-only VA vs DCA benchmark."""
    import va_backtest

    bundled = script_dir / "data" / "source_adjusted_close.csv"
    if refresh_prices:
        market_result = refresh_market_data(script_dir)
        refresh_info = market_result["refresh_info"]
        advice_context = market_result["advice_context"]
        advice_warning = market_result["advice_warning"]
    else:
        refresh_info = advice_context = advice_warning = None
        print("仅使用本地既有行情离线重算；不会联网更新数据。")
    source_label = (
        f"Yahoo Finance，经 yfinance 本次联网获取的 VOO/QQQM 自动复权日收盘价；"
        f"共同交易日截至 {refresh_info['latest_date']}"
        if refresh_info else "本地既有 VOO/QQQM 自动复权日收盘价；本轮未联网刷新"
    )
    prices = va_backtest.load_prices(bundled)
    vap = config["va_plan"]
    step_total = sum((D(vap["monthly_steps_usd"][symbol]) for symbol in SYMBOLS), Decimal(0))
    weights = (
        float(D(vap["monthly_steps_usd"]["VOO"]) / step_total),
        float(D(vap["monthly_steps_usd"]["QQQM"]) / step_total),
    )
    study = va_backtest.run_full_study(
        prices,
        config["va_backtest_output_dir"],
        float(vap["initial_targets_usd"]["VOO"]),
        float(vap["initial_targets_usd"]["QQQM"]),
        float(vap["monthly_steps_usd"]["VOO"]),
        float(vap["monthly_steps_usd"]["QQQM"]),
        int(config["backtest_execution_day"]),
        weights,
        source_label,
    )
    report = Path(study["report"])
    note = sync_va_backtest_summary(
        report, config["va_backtest_output_dir"], config, refreshed=refresh_prices
    )
    print(f"独立 VA vs DCA 研究基准报告：{report}")
    result = {"report": report, "last_date": report.stem.rsplit("_", 1)[-1],
              "refreshed": refresh_prices, "refresh_info": refresh_info,
              "advice_context": advice_context, "advice_warning": advice_warning,
              "metrics": study["metrics"], "obsidian_note": note}
    result["retention_cleanup"] = cleanup_generated_reports(config, protect=(report,))
    cleanup = result["retention_cleanup"]
    if cleanup["removed_count"]:
        print(f"已自动回收 {cleanup['removed_count']} 份旧回测报告。")
    for warning in cleanup["warnings"]:
        print(f"生成文件回收提示：{warning}")
    return result


def refresh_after_change(con: sqlite3.Connection, config: dict, action: str) -> None:
    print(f"{action}已保存到 SQLite。")
    try:
        refresh(con, config)
    except (ValueError, OSError, UnicodeError) as exc:
        print(f"流水已保存，但报告或 Obsidian 同步失败：{exc}。修复后选 5 生成 HTML，或选 6 同步 Obsidian。")


CSV_FIELDS = ("date", "kind", "symbol", "amount", "price", "shares", "fee", "note",
              "source_type", "plan_month", "trigger_note", "transfer_date", "hkd_sent",
              "fx_rate_hkd_per_usd", "fx_cost_hkd")


def export_csv(con: sqlite3.Connection, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=("id",) + CSV_FIELDS)
        writer.writeheader()
        for r in rows(con):
            writer.writerow({key: r[key] for key in ("id",) + CSV_FIELDS})
    return path


def import_csv(con: sqlite3.Connection, path: Path) -> int:
    if rows(con):
        raise ValueError("为避免重复导入，仅允许向空账本导入 CSV；请先备份并使用新数据库。")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or any(key not in reader.fieldnames for key in CSV_FIELDS[:8]):
            raise ValueError("CSV 缺少基础流水列。")
        records = []
        for raw in reader:
            records.append({key: raw.get(key) or None for key in CSV_FIELDS})
    if records:
        add_records_batch(con, records)
    return len(records)


def record_rule_review(con: sqlite3.Connection, config: dict) -> dict:
    symbol = ask("观察标的 VOO/QQQM").upper()
    if symbol not in SYMBOLS:
        raise ValueError("标的须为 VOO 或 QQQM。")
    observation = {"date": parse_day(ask("观察日期 YYYY-MM-DD", date.today().isoformat()))}
    for key, label in (("drawdown_52w_pct", "距52周高点回撤 %"), ("vix", "VIX"),
                       ("yield_10y_pct", "美国10年期收益率 %")):
        raw = ask(label + "（未知留空）")
        observation[key] = decimal_text(D(raw)) if raw else None
    for span in (20, 50, 200):
        raw = ask(f"相对{span}日均线：above 高于 / below 低于 / 留空未知")
        if raw and raw not in ("above", "below"):
            raise ValueError("均线位置须为 above、below 或留空。")
        observation[f"ma{span}_position"] = raw or None
    observation["valuation_note"] = ask("估值与盈利逻辑备注（可留空）")
    observation["manual_confirmation"] = ask("人工确认本次观察？Y/N", "N").upper() == "Y"
    outcome = evaluate_rules(observation, config["buy_rules"])
    with con:
        con.execute("INSERT INTO rule_reviews(date,symbol,observation_json,evaluation_json) VALUES (?,?,?,?)",
                    (observation["date"], symbol, json.dumps(observation, ensure_ascii=False),
                     json.dumps(outcome, ensure_ascii=False)))
    return outcome


def edit_config(config_file: Path, config: dict) -> None:
    with config_file.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    raw["active_plan"] = "independent_va_only"
    raw["investment_mode"] = "va"
    raw["budget_day"] = int(ask("每月预算确定日（1–28）", str(config["budget_day"])))
    raw.pop("target_day", None)
    va = config["va_plan"]
    raw["va_plan"] = {
        "start_month": ask("VA 起始月份 YYYY-MM", va["start_month"]),
        "initial_targets_usd": {
            symbol: ask(
                f"{symbol} 首月目标价值 USD",
                va["initial_targets_usd"][symbol],
            ) for symbol in SYMBOLS
        },
        "monthly_steps_usd": {
            symbol: ask(
                f"{symbol} 每月目标步长 USD",
                va["monthly_steps_usd"][symbol],
            ) for symbol in SYMBOLS
        },
        "sell_if_above_target": False,
    }
    raw["generated_report_keep_count"] = int(ask(
        "每类 VA 历史报告保留份数（1–30）",
        str(config["generated_report_keep_count"]),
    ))
    temporary = config_file.with_name(config_file.name + ".check")
    try:
        atomic_text(temporary, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
        checked = load_config(temporary)
    finally:
        temporary.unlink(missing_ok=True)
    atomic_text(config_file, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
    config.clear()
    config.update(load_config(config_file))


def _legacy_menu(con: sqlite3.Connection, config: dict, script_dir: Path,
                 config_file: Path) -> None:
    while True:
        print("\nVOO / QQQM 投资工具")
        print("1 录入入金   2 录入买入/卖出   3 本月定投进度   4 查看持仓")
        print("5 查看额外资金池   6 生成中文 HTML   7 同步 Obsidian   8 导出 CSV")
        print("9 修改配置   10 积极买入条件观察   11 查看/修改/删除流水")
        print("12 录入参考价   13 历史回测   14 导入 CSV   15 其他流水   0 退出")
        choice = input("请选择：").strip()
        try:
            if choice == "1":
                add_or_edit(con, prompt_record(kind_override="deposit"), va_config=config)
                refresh_after_change(con, config, "入金")
            elif choice == "2":
                trade_kind = ask("买入 buy / 卖出 sell", "buy").lower()
                if trade_kind not in ("buy", "sell"):
                    raise ValueError("请选择 buy 或 sell。")
                add_or_edit(con, prompt_record(kind_override=trade_kind), va_config=config)
                refresh_after_change(con, config, "交易")
            elif choice == "3":
                metrics = plan_and_pool(rows(con), config)
                month = metrics["current_month"]
                print(f"{month} 预算日：{config['budget_day']} 日；月内可分批买入。已买入 ${fmt(metrics['current_fixed_buy'])}")
                print(f"实际到账：{metrics['fixed_arrivals'].get(month) or '尚无记录'}；最后成交：{metrics['fixed_trades'].get(month) or '尚无记录'}")
                print(f"已到期月份完成：{metrics['completed_months']}/{metrics['eligible_months']}（{metrics['fixed_completion_pct']:.1f}%）")
            elif choice == "4":
                state = calculate(rows(con), get_quotes(con))
                print(f"累计入金 ${fmt(state['deposits'])}｜累计买入 ${fmt(state['buys'])}｜现金 ${fmt(state['cash'])}｜已实现 ${fmt(state['realized_total'])}｜浮盈亏 ${fmt(state['unrealized'])}")
                for symbol in SYMBOLS:
                    p, v = state["positions"][symbol], state["valuations"][symbol]
                    ratio = v["value"] / state["market_value"] * 100 if state["market_value"] else Decimal(0)
                    print(f"{symbol}：{fmt(p.shares, 8)} 股，平均成本 ${fmt(p.basis / p.shares if p.shares else Decimal(0), 4)}，参考市值 ${fmt(v['value'])}，实际比例 {ratio:.1f}%")
            elif choice == "5":
                metrics = plan_and_pool(rows(con), config)
                print(f"额外资金余额 ${fmt(metrics['pool']['extra'])}；累计入金 ${fmt(metrics['extra_deposits'])}；累计买入 ${fmt(metrics['extra_buys'])}；累计买入/入金 {metrics['extra_usage_pct']:.1f}%")
                print(f"最早未使用资金：{metrics['extra_oldest_date'] or '无'}，已等待 {metrics['extra_wait_days']} 天；闲置资金累计等待 {fmt(metrics['extra_wait_usd_days'])} 美元·天，加权平均 {metrics['extra_avg_wait_days']:.1f} 天；未分类现金 ${fmt(metrics['pool']['legacy'])}")
            elif choice == "6":
                refresh(con, {**config, "obsidian_sync_enabled": False})
            elif choice == "7":
                state = calculate(rows(con), get_quotes(con))
                location = sync_obsidian(
                    rows(con), state, config, opportunity_usd_summary(con, config)
                )
                print(f"Obsidian 已同步：{location}" if location else "Obsidian 同步尚未启用，请在配置中设置 obsidian_sync_enabled=true。")
            elif choice == "8":
                path = export_csv(con, script_dir / "exports" / "transactions.csv")
                print(f"CSV 已导出：{path}")
            elif choice == "9":
                edit_config(config_file, config)
                print("配置已保存。")
                refresh(con, config)
            elif choice == "10":
                outcome = record_rule_review(con, config)
                print(f"条件观察：{outcome['status']}。这只是人工复核记录，不会生成交易指令。")
                print(json.dumps(outcome["checks"], ensure_ascii=False, indent=2))
                refresh(con, {**config, "obsidian_sync_enabled": False})
            elif choice == "11":
                print_records(rows(con))
                action = ask("输入 E 修改 / D 删除 / 回车返回").upper()
                if action in ("E", "D"):
                    record_id = int(ask("记录 ID"))
                    old = con.execute("SELECT * FROM transactions WHERE id=?", (record_id,)).fetchone()
                    if not old:
                        raise ValueError("记录 ID 不存在。")
                    if action == "E":
                        add_or_edit(
                            con, prompt_record(old), record_id, va_config=config
                        )
                        refresh_after_change(con, config, "流水修改")
                    elif ask(f"确认删除 #{record_id}？输入 DELETE") == "DELETE":
                        delete_record(con, record_id)
                        refresh_after_change(con, config, "流水删除")
            elif choice == "12":
                symbol = ask("标的 VOO/QQQM").upper()
                if symbol not in SYMBOLS:
                    raise ValueError("标的须为 VOO 或 QQQM。")
                quote_date = parse_day(ask("参考价日期 YYYY-MM-DD", date.today().isoformat()))
                price = D(ask("参考价 USD/股"))
                if price <= 0:
                    raise ValueError("参考价须大于零。")
                with con:
                    con.execute("INSERT INTO quotes(symbol,as_of,price,source) VALUES (?,?,?,'manual') ON CONFLICT(symbol) DO UPDATE SET as_of=excluded.as_of, price=excluded.price, source=excluded.source", (symbol, quote_date, decimal_text(price)))
                refresh_after_change(con, config, "参考价")
            elif choice == "13":
                result = run_backtest(config, script_dir, include_opportunity=True)
                quote_sync = sync_market_quotes(
                    con, script_dir / "data" / "source_adjusted_close.csv",
                    expected_as_of=result["refresh_info"]["latest_date"],
                )
                refresh(con, config)
                print(f"持仓估算参考价已按 {quote_sync['as_of']} 的最新可用行情重新计算。")
            elif choice == "14":
                count = import_csv(con, Path(ask("CSV 文件完整路径")).expanduser())
                refresh_after_change(con, config, f"{count} 条流水导入")
            elif choice == "15":
                add_or_edit(con, prompt_record(), va_config=config)
                refresh_after_change(con, config, "流水")
            elif choice == "0":
                return
            else:
                print("请选择 0–15。")
        except (ValueError, sqlite3.Error, OSError, ImportError) as exc:
            print(f"未完成：{exc}")


def menu(con: sqlite3.Connection, config: dict, script_dir: Path,
         config_file: Path) -> None:
    """Fallback terminal menu for the same single independent-VA workflow."""
    while True:
        print("\nVOO / QQQM 独立 VA 投资工具")
        print("1 录入入金   2 录入买入/卖出   3 查看 VA 目标   4 查看持仓")
        print("5 生成中文 HTML   6 同步 Obsidian   7 导出 CSV   8 修改 VA 配置")
        print("9 查看/修改/删除流水   10 录入参考价   11 更新行情并回测")
        print("12 导入 CSV   13 其他流水   0 退出")
        choice = input("请选择：").strip()
        try:
            if choice == "1":
                add_or_edit(con, prompt_record(kind_override="deposit"), va_config=config)
                refresh_after_change(con, config, "入金")
            elif choice == "2":
                trade_kind = ask("买入 buy / 卖出 sell", "buy").lower()
                if trade_kind not in ("buy", "sell"):
                    raise ValueError("请选择 buy 或 sell。")
                add_or_edit(con, prompt_record(kind_override=trade_kind), va_config=config)
                refresh_after_change(con, config, "交易")
            elif choice == "3":
                state = calculate(rows(con), get_quotes(con))
                va = va_summary(state, config)
                label = "下次预览" if va["preview"] else "本月"
                print(f"{va['month']} {label}｜每月 {config['budget_day']} 日确定预算，月内分批")
                for symbol in SYMBOLS:
                    item = va["symbols"][symbol]
                    print(
                        f"{symbol}：目标 {usd(item['target_value_usd'])}｜"
                        f"参考市值 {usd(item['current_market_value_usd'])}｜"
                        f"理论投入 {usd(item['suggested_buy_usd'])}"
                    )
                print(f"合计理论投入：{usd(va['suggested_total_usd'])}")
            elif choice == "4":
                state = calculate(rows(con), get_quotes(con))
                print(
                    f"累计入金 {usd(state['deposits'])}｜市场累计投入 {usd(state['buys'])}｜"
                    f"现金 {usd(state['cash'])}｜已实现 {usd(state['realized_total'])}｜"
                    f"未实现 {usd(state['unrealized'])}"
                )
                for symbol in SYMBOLS:
                    position = state["positions"][symbol]
                    valuation = state["valuations"][symbol]
                    average = position.basis / position.shares if position.shares else Decimal(0)
                    print(
                        f"{symbol}：{fmt(position.shares, 8)} 股｜平均成本 {usd(average, 4)}｜"
                        f"参考市值 {usd(valuation['value'])}｜价格日期 {valuation['date'] or '—'}"
                    )
            elif choice == "5":
                refresh(con, {**config, "obsidian_sync_enabled": False})
            elif choice == "6":
                state = calculate(rows(con), get_quotes(con))
                location = sync_obsidian(rows(con), state, config)
                print(
                    f"Obsidian 已同步：{location}" if location else
                    "Obsidian 同步尚未启用。"
                )
            elif choice == "7":
                path = export_csv(con, script_dir / "exports" / "transactions.csv")
                print(f"CSV 已导出：{path}")
            elif choice == "8":
                edit_config(config_file, config)
                print("VA 配置已保存。")
                refresh(con, config)
            elif choice == "9":
                print_records(rows(con))
                action = ask("输入 E 修改 / D 删除 / 回车返回").upper()
                if action in ("E", "D"):
                    record_id = int(ask("记录 ID"))
                    old = con.execute(
                        "SELECT * FROM transactions WHERE id=?", (record_id,)
                    ).fetchone()
                    if not old:
                        raise ValueError("记录 ID 不存在。")
                    if action == "E":
                        add_or_edit(
                            con, prompt_record(old), record_id, va_config=config
                        )
                        refresh_after_change(con, config, "流水修改")
                    elif ask(f"确认删除 #{record_id}？输入 DELETE") == "DELETE":
                        delete_record(con, record_id)
                        refresh_after_change(con, config, "流水删除")
            elif choice == "10":
                symbol = ask("标的 VOO/QQQM").upper()
                quote_date = ask("参考价日期 YYYY-MM-DD", date.today().isoformat())
                result = set_manual_quote(con, symbol, quote_date, ask("参考价 USD/股"))
                refresh_after_change(con, config, "参考价" if result["updated"] else "参考价核对")
            elif choice == "11":
                result = run_backtest(config, script_dir, refresh_prices=True)
                quote_sync = sync_market_quotes(
                    con, script_dir / "data" / "source_adjusted_close.csv",
                    expected_as_of=result["refresh_info"]["latest_date"],
                )
                refresh(con, config)
                print(f"持仓估算参考价已核对至 {quote_sync['as_of']}。")
            elif choice == "12":
                count = import_csv(con, Path(ask("CSV 文件完整路径")).expanduser())
                refresh_after_change(con, config, f"{count} 条流水导入")
            elif choice == "13":
                add_or_edit(con, prompt_record(), va_config=config)
                refresh_after_change(con, config, "流水")
            elif choice == "0":
                return
            else:
                print("请选择 0–13。")
        except (ValueError, sqlite3.Error, OSError, ImportError) as exc:
            print(f"未完成：{exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"), help="JSON 配置文件路径")
    parser.add_argument("--report", action="store_true", help="仅更新真实投资报告和 Obsidian")
    parser.add_argument("--backtest", action="store_true", help="联网更新行情后运行历史回测")
    parser.add_argument("--backtest-offline", action="store_true", help="仅使用既有本地行情离线重算")
    parser.add_argument("--quick", action="store_true", help="快捷录入 VA 入金与多笔真实买入")
    args = parser.parse_args()
    config_file = args.config.expanduser().resolve()
    config = load_config(config_file)
    if args.backtest or args.backtest_offline:
        if args.backtest and args.backtest_offline:
            parser.error("--backtest 与 --backtest-offline 只能选一个。")
        script_dir = Path(__file__).resolve().parent
        result = run_backtest(
            config, script_dir, refresh_prices=not args.backtest_offline,
        )
        if args.backtest:
            with connect(config["database_path"]) as con:
                quote_sync = sync_market_quotes(
                    con, script_dir / "data" / "source_adjusted_close.csv",
                    expected_as_of=result["refresh_info"]["latest_date"],
                )
                refresh(con, config)
            print(f"持仓估算参考价已按 {quote_sync['as_of']} 的最新可用行情重新计算。")
        return
    with connect(config["database_path"]) as con:
        if args.quick:
            if quick_entry(con, config):
                refresh_after_change(con, config, "快捷记账")
            return
        refresh(con, config)
        if not args.report:
            menu(con, config, Path(__file__).resolve().parent, config_file)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, sqlite3.Error, json.JSONDecodeError, ImportError) as exc:
        print(f"运行失败：{exc}", file=sys.stderr)
        sys.exit(1)
