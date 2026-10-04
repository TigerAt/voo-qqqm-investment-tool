"""Offline historical VOO/QQQM backtests, separate from the real trade ledger.

The calculations are delegated to the local V1.3 backtest engine included next to
this file. ``ratios`` lists the VOO percentage of each simulated portfolio;
QQQM receives the remainder. The default retains V1.3's five portfolios and its
full original Chinese report.
"""

from __future__ import annotations

import base64
import os
import tempfile
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__:
    from . import voo_qqqm_backtest_v1_3 as original
else:
    import voo_qqqm_backtest_v1_3 as original


DEFAULT_RATIOS = [100, 80, 60, 40, 0]


def latest_completed_us_day(now: datetime | None = None) -> date:
    """Conservatively omit today's daily bar until the US close has settled."""
    eastern = (now or datetime.now(ZoneInfo("America/New_York"))).astimezone(
        ZoneInfo("America/New_York"))
    cutoff = eastern.date() if eastern.time() >= time(20) else eastern.date() - timedelta(days=1)
    while cutoff.weekday() >= 5:
        cutoff -= timedelta(days=1)
    return cutoff


def refresh_source_prices(source_csv: Path, cutoff: date | None = None) -> dict[str, str | bool | None]:
    """Download complete adjusted daily histories; replace the CSV only after validation."""
    try:
        import yfinance as yf
    except ImportError as exc:
        raise ValueError("缺少 yfinance，无法联网更新行情；请安装 requirements.txt 中的依赖。") from exc
    from market_network import direct_yfinance_session

    cutoff = cutoff or latest_completed_us_day()
    end_exclusive = (cutoff + timedelta(days=1)).isoformat()
    session = direct_yfinance_session()
    parts = []
    raw_parts = []
    actions = []
    for symbol in ("VOO", "QQQM"):
        try:
            history = yf.Ticker(symbol, session=session).history(
                start="2020-10-13", end=end_exclusive, interval="1d",
                auto_adjust=False, actions=True, keepna=True, timeout=20,
            ).copy()
        except Exception as exc:
            raise ValueError(f"{symbol} 行情下载失败：{exc}。本地行情未改动。") from exc
        if history.empty or "Close" not in history or "Adj Close" not in history:
            raise ValueError(f"{symbol} 行情下载为空。本地行情未改动。")
        raw_series = history["Close"].rename(symbol)
        raw_series.index = original.pd.DatetimeIndex(raw_series.index).tz_localize(None).normalize()
        split_series = history.get("Stock Splits")
        if split_series is not None:
            for stamp,ratio in split_series.items():
                if original.pd.notna(ratio) and float(ratio) != 0:
                    if float(ratio)<=0:raise ValueError("拆股比例无效，本地行情未改动。")
                    day=stamp.date()
                    actions.append({"symbol":symbol,"date":day.isoformat(),"ratio":str(float(ratio)),"type":"split"})
                    # Yahoo Close is split-adjusted. Reconstruct the historical
                    # nominal share price for budget-day shares before each split.
                    raw_series.loc[raw_series.index < original.pd.Timestamp(day)] *= float(ratio)
        raw_parts.append(raw_series)
        series = history["Adj Close"].rename(symbol)
        series.index = original.pd.DatetimeIndex(series.index).tz_localize(None).normalize()
        parts.append(series)

    # Yahoo can publish a row for the latest session before its adjusted close is
    # ready.  In that case both symbols appear with NaN, even though every earlier
    # row is complete.  Remove only incomplete rows at the end; an incomplete row
    # inside the history still fails validation so a genuine data gap is never
    # silently hidden.
    combined = original.pd.concat(parts, axis=1, join="outer").sort_index()
    while not combined.empty and combined.iloc[-1].isna().any():
        combined = combined.iloc[:-1]

    try:
        prices = original.validate_prices(combined, end=cutoff)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"下载的共同交易日数据未通过校验：{exc}。本地行情未改动。") from exc
    raw_combined = original.pd.concat(raw_parts,axis=1,join="outer").sort_index().loc[prices.index]
    raw_prices = original.validate_prices(raw_combined,end=cutoff)
    latest = prices.index[-1].date()
    if latest > cutoff:
        raise ValueError("行情含尚未收盘的日期，本地行情未改动。")
    import exchange_calendars as xcals
    calendar=xcals.get_calendar("XNYS")
    expected=calendar.date_to_session(original.pd.Timestamp(cutoff),direction="previous").date()
    if latest != expected:
        raise ValueError(f"行情截至 {latest}，应为 {expected}；本地行情未改动。")

    source_csv = Path(source_csv).expanduser()
    previous = None
    unchanged = False
    if source_csv.exists() or (source_csv.parent / "market_prices_manifest.json").exists():
        old = None
        try:
            from market_prices import resolve
            old = original.pd.read_csv(resolve(source_csv), parse_dates=["Date"], index_col="Date")
            old = original.validate_prices(old)
            previous = old.index[-1].date()
        except (ValueError, OSError, KeyError) as exc:
            # A validated full download can repair a corrupt snapshot. Keep the
            # committed date as a lower bound so repair never rolls data back.
            import json
            manifest_path = source_csv.parent / "market_prices_manifest.json"
            if not manifest_path.exists():
                raise ValueError(f"原行情文件无法核对：{exc}。请恢复备份后再更新。") from exc
            try:
                previous = date.fromisoformat(json.loads(manifest_path.read_text())["as_of"])
            except (ValueError, KeyError, OSError) as metadata_error:
                raise ValueError("行情快照及日期损坏，请恢复备份。") from metadata_error
        if latest < previous:
            raise ValueError(f"下载结果仅截至 {latest}，比本地 {previous} 更旧；本地行情未改动。")
        unchanged = old is not None and old.equals(prices)

    from market_prices import publish
    source_csv.parent.mkdir(parents=True,exist_ok=True)
    publish(source_csv.parent,raw_prices,prices,actions)
    return {
        "latest_date": latest.isoformat(),
        "previous_date": previous.isoformat() if previous else None,
        "cutoff_date": cutoff.isoformat(),
        "changed": not unchanged,
    }


def _amount(value: Decimal, name: str, *, positive: bool) -> float:
    try:
        amount = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{name} 必须是有效金额。") from exc
    if not amount.is_finite() or (amount <= 0 if positive else amount < 0):
        rule = "大于零" if positive else "不小于零"
        raise ValueError(f"{name} 必须是有限且{rule}的金额。")
    return float(amount)


def _ratios(ratios: list[int] | None) -> list[int]:
    if ratios is None:
        return DEFAULT_RATIOS.copy()
    if not ratios or any(type(r) is not int or r < 0 or r > 100 for r in ratios):
        raise ValueError("配置比例须为 0 至 100 的整数列表，表示 VOO 占比。")
    if len(set(ratios)) != len(ratios):
        raise ValueError("配置比例不能重复。")
    return ratios.copy()


def _target_day(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 28:
        raise ValueError("每月目标日须为 1 至 28 的整数。")
    return value


def _custom_report(out_dir: Path, prices, summary, source: str) -> Path:
    """Render a ratio-independent, standalone report for a custom set of splits."""
    end = prices.index[-1].strftime("%Y-%m-%d")
    start = prices.index[0].strftime("%Y-%m-%d")
    columns = [
        ("portfolio", "VOO/QQQM", str),
        ("contributed_usd", "累计投入", original.fmt_money),
        ("ending_usd", "期末资产", original.fmt_money),
        ("profit_usd", "投资盈亏", original.fmt_money),
        ("money_weighted_xirr", "XIRR", original.fmt_pct),
        ("time_weighted_cagr", "TWR 年化", original.fmt_pct),
        ("max_twr_drawdown", "最大回撤", original.fmt_pct),
        ("actual_qqqm_weight_end", "期末 QQQM 实际占比", original.fmt_abs_pct),
    ]
    heads = "".join(f"<th>{escape(label)}</th>" for _, label, _ in columns)
    rows = []
    for _, row in summary.iterrows():
        cells = "".join(
            f"<td>{escape(str(formatter(row[key])))}</td>"
            for key, _, formatter in columns
        )
        rows.append(f"<tr>{cells}</tr>")
    account_chart = base64.b64encode((out_dir / "account_value.png").read_bytes()).decode("ascii")
    drawdown_chart = base64.b64encode((out_dir / "drawdown.png").read_bytes()).decode("ascii")
    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>VOO / QQQM 历史回测｜{end}</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f4f7fc;color:#17243c;font:15px/1.7 -apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif}}
main{{max-width:1120px;margin:auto;padding:24px 16px 48px}}header{{background:#17345f;color:#fff;padding:28px;border-radius:16px}}h1{{margin:0 0 4px;font-size:27px}}h2{{margin:0 0 12px;font-size:20px}}
section{{background:#fff;border:1px solid #e0e7f1;border-radius:15px;padding:22px;margin-top:17px}}.muted{{color:#5b6a81}}.table{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;min-width:850px}}
td,th{{padding:9px 8px;border-bottom:1px solid #e6ecf4;text-align:right;white-space:nowrap}}td:first-child,th:first-child{{text-align:left}}th{{background:#eef4ff}}img{{width:100%;height:auto;display:block;border:1px solid #e6ecf4;border-radius:10px;margin-top:14px}}
li{{margin:7px 0}}@media print{{body{{background:white}}section{{break-inside:avoid}}}}
</style></head><body><main>
<header><h1>VOO / QQQM 历史回测</h1><div>{start} 至 {end} · {len(prices):,} 个共同交易日 · 自定义配置</div></header>
<section><h2>模拟结果</h2><p>首个共同交易日独立投入额外本金 ${original.INITIAL:,.2f}；历史研究全区间每月固定投入 ${original.MONTHLY:,.2f}，在每月 {original.TARGET_DAY} 日当天或之后的首个共同交易日执行；若该月没有符合日期则使用当月最后一个共同交易日。该恒定研究金额与真实账户按月份生效的定投计划分别管理。</p>
<div class="table"><table><thead><tr>{heads}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p class="muted">每笔新投入按表中比例买入，持仓不卖出再平衡，因此期末实际比例会漂移。账户资产曲线包含追加本金；最大回撤使用剔除追加本金影响的时间加权净值。</p>
<img alt="账户资产曲线" src="data:image/png;base64,{account_chart}"><img alt="时间加权净值回撤曲线" src="data:image/png;base64,{drawdown_chart}"></section>
<section><h2>方法与数据</h2><ul>
<li>价格来源：{escape(source)}。使用 VOO 与 QQQM 的共同交易日历史复权收盘价；未逐日独立核验原始报价。</li>
<li>按当日复权收盘价买入，可持有零碎份额；复权价格已反映分红及拆分调整，不重复计入分红。</li>
<li>不计手续费、税费、买卖价差、汇兑或跨境汇款成本；历史结果不预测未来收益。</li>
<li>XIRR 使用每次实际模拟投入日期和回测结束日；TWR 排除外部投入影响。</li>
</ul></section></main></body></html>"""
    path = out_dir / f"voo_qqqm_backtest_report_{end}.html"
    path.write_text(html, encoding="utf-8")
    return path


def run_backtest(
    price_csv: Path,
    out_dir: Path,
    initial: Decimal,
    monthly: Decimal,
    ratios: list[int] | None = None,
    source_label: str | None = None,
    target_day: int = 10,
) -> Path:
    """Run V1.3's simulation offline and return the standalone Chinese HTML report.

    The output directory contains a copy of the exact prices used, a summary CSV,
    daily portfolio CSVs, and charts. No live investment record is read or changed.
    """
    price_csv = Path(price_csv).expanduser()
    out_dir = Path(out_dir).expanduser()
    if not price_csv.is_file():
        raise FileNotFoundError(f"找不到历史行情文件：{price_csv}")
    if out_dir.resolve() == price_csv.resolve().parent:
        raise ValueError("回测结果目录必须与历史行情数据目录分开。")
    initial_float = _amount(initial, "首月额外本金", positive=False)
    monthly_float = _amount(monthly, "每月定投金额", positive=True)
    chosen = _ratios(ratios)
    chosen_target_day = _target_day(target_day)

    prices = original.pd.read_csv(price_csv, parse_dates=["Date"], index_col="Date")
    prices = original.validate_prices(prices)
    out_dir.mkdir(parents=True, exist_ok=True)
    original.INITIAL, original.MONTHLY, original.TARGET_DAY = (
        initial_float, monthly_float, chosen_target_day)
    prices.to_csv(out_dir / "source_adjusted_close.csv", index_label="Date")
    (out_dir / "model_parameters.txt").write_text(
        f"initial_extra_usd={initial_float}\n"
        f"backtest_monthly_budget_usd={monthly_float}\n"
        f"monthly_budget_mode=constant_for_full_history\n"
        f"first_month_total_usd={initial_float + monthly_float}\n"
        f"target_day={chosen_target_day}\n"
        f"voo_percentages={','.join(map(str, chosen))}\n",
        encoding="utf-8",
    )
    summaries, details = [], {}
    for voo_percent in chosen:
        metrics, daily = original.simulate(prices, (voo_percent / 100, (100 - voo_percent) / 100))
        summaries.append(metrics)
        label = metrics["portfolio"]
        details[label] = daily
        daily.to_csv(out_dir / f"portfolio_{label.replace('/', '_')}_daily.csv", index_label="Date")
    summary = original.pd.DataFrame(summaries)
    summary.to_csv(out_dir / "five_portfolios_summary.csv", index=False)
    original.create_charts(out_dir, details)
    source = source_label or "本地历史复权价格 CSV；本次离线复算，未联网刷新行情"
    if chosen == DEFAULT_RATIOS:
        return original.render_report(out_dir, prices, summary, details, source)
    return _custom_report(out_dir, prices, summary, source)
