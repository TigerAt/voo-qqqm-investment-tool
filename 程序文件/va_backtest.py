#!/usr/bin/env python3
"""VOO / QQQM independent Value Averaging vs fixed DCA historical research.

The real plan treats VOO and QQQM as two independent VA paths:

* VOO first target USD 500, then target +USD 500 each month.
* QQQM first target USD 300, then target +USD 300 each month.
* Buy only. If market value is already above a symbol's target, that symbol buys 0.
* No combined monthly cap and no cross-subsidy between the two independent paths.

The only benchmark in the main study is fixed-dollar DCA using the same nominal
monthly amount for each symbol (VOO USD 500 / QQQM USD 300 by default).  This
keeps the research focused on the user's actual independent-VA execution rule
and a conventional DCA baseline.
"""
from __future__ import annotations

import argparse
import base64
import json
from html import escape
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import voo_qqqm_backtest_v1_3 as original
except ImportError:  # pragma: no cover
    from . import voo_qqqm_backtest_v1_3 as original

SYMBOLS = ("VOO", "QQQM")


def load_prices(path: Path) -> pd.DataFrame:
    from market_prices import resolve
    path = resolve(path)
    df = pd.read_csv(path, parse_dates=["Date"]).set_index("Date").sort_index()
    missing = [s for s in SYMBOLS if s not in df.columns]
    if missing:
        raise ValueError(f"行情缺少列：{', '.join(missing)}")
    px = df[list(SYMBOLS)].apply(pd.to_numeric, errors="coerce").dropna()
    if px.empty or (px <= 0).any().any():
        raise ValueError("VOO/QQQM 行情为空或包含非正价格。")
    return px


def monthly_dates(prices: pd.DataFrame, day: int = 10) -> list[pd.Timestamp]:
    if type(day) is not int or not 1 <= day <= 28:
        raise ValueError("每月目标日须为 1–28 的整数。")
    out: list[pd.Timestamp] = []
    groups = list(prices.groupby(prices.index.to_period("M")))
    for index, (_, group) in enumerate(groups):
        eligible = group[group.index.day >= day]
        if len(eligible):
            out.append(eligible.index[0])
        elif index < len(groups) - 1:
            # A completed historical month with an unusually sparse input still
            # uses its final available common trading day.  The terminal month is
            # different: when the data has not reached the target day yet, the
            # contribution has not happened and must not be moved into the past.
            out.append(group.index[-1])
    return out


def _metrics_from_daily(daily: pd.DataFrame, label: str) -> dict:
    account = daily["account_value"].astype(float)
    deposits = daily["external_deposit"].astype(float)
    returns = pd.Series(0.0, index=daily.index, dtype=float)
    previous = account.shift(1)
    funded = previous > 0
    returns.loc[funded] = (account.loc[funded] - deposits.loc[funded]) / previous.loc[funded] - 1.0
    nav = (1.0 + returns).cumprod()
    dd = nav / nav.cummax() - 1.0
    daily["twr_nav"] = nav
    daily["twr_drawdown"] = dd

    period_years = max((daily.index[-1] - daily.index[0]).days / 365.25, 1 / 365.25)
    contributed = float(deposits.sum())
    terminal = float(account.iloc[-1])
    positives = deposits[deposits > 0]
    xirr = (
        original.xirr(positives.to_numpy(), terminal, list(positives.index), daily.index[-1])
        if len(positives) else np.nan
    )
    episode = original.drawdown_episode(nav)
    return {
        "strategy": label,
        "start": str(daily.index[0].date()),
        "end": str(daily.index[-1].date()),
        "contributed_usd": round(contributed, 2),
        "ending_usd": round(terminal, 2),
        "profit_usd": round(terminal - contributed, 2),
        "money_weighted_xirr": float(xirr) if not pd.isna(xirr) else np.nan,
        "time_weighted_cagr": float(nav.iloc[-1] ** (1 / period_years) - 1),
        "annualized_volatility": float(returns.iloc[1:].std(ddof=1) * np.sqrt(252)),
        "max_twr_drawdown": float(dd.min()),
        "num_contributions": int((deposits > 0).sum()),
        "avg_contribution_usd": round(float(positives.mean()), 2) if len(positives) else 0.0,
        "max_contribution_usd": round(float(positives.max()), 2) if len(positives) else 0.0,
        "min_positive_contribution_usd": round(float(positives.min()), 2) if len(positives) else 0.0,
        "p90_contribution_usd": round(float(positives.quantile(0.90)), 2) if len(positives) else 0.0,
        "p95_contribution_usd": round(float(positives.quantile(0.95)), 2) if len(positives) else 0.0,
        "p99_contribution_usd": round(float(positives.quantile(0.99)), 2) if len(positives) else 0.0,
        "max_dd_peak_date": episode["peak"],
        "max_dd_trough_date": episode["trough"],
        "max_dd_recovery_date": episode["recovery"],
        "max_dd_days_underwater": episode["days_underwater"],
    }


def simulate_symbol_va(
    prices: pd.DataFrame,
    symbol: str,
    initial_target: float,
    monthly_step: float,
    target_day: int = 10,
) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    """Run one symbol's buy-only VA path independently."""
    if symbol not in SYMBOLS:
        raise ValueError("symbol must be VOO or QQQM")
    if initial_target <= 0 or monthly_step <= 0:
        raise ValueError("VA 初始目标和月步长须大于零。")

    schedule = monthly_dates(prices, target_day)
    month_index = {dt: i for i, dt in enumerate(schedule)}
    shares = 0.0
    daily_rows = []
    monthly_rows = []
    for dt, price in prices[symbol].items():
        price = float(price)
        pre_value = shares * price
        deposit = 0.0
        if dt in month_index:
            n = month_index[dt]
            target = initial_target + monthly_step * n
            gap = max(0.0, target - pre_value)
            deposit = gap
            if deposit > 0:
                shares += deposit / price
            post_value = shares * price
            monthly_rows.append({
                "Date": dt,
                "month_no": n + 1,
                "symbol": symbol,
                "target_value": target,
                "pre_value": pre_value,
                "gap": gap,
                "buy": deposit,
                "post_value": post_value,
                "shares": shares,
                "price": price,
            })
        daily_rows.append({"Date": dt, "account_value": shares * price, "external_deposit": deposit})

    daily = pd.DataFrame(daily_rows).set_index("Date")
    monthly = pd.DataFrame(monthly_rows)
    metrics = _metrics_from_daily(daily, f"{symbol} 独立VA")
    metrics["symbol"] = symbol
    metrics["initial_target_usd"] = initial_target
    metrics["monthly_step_usd"] = monthly_step
    return metrics, daily, monthly


def simulate_symbol_dca(
    prices: pd.DataFrame,
    symbol: str,
    monthly_budget: float,
    target_day: int = 10,
) -> tuple[dict, pd.DataFrame]:
    """Conventional fixed-dollar DCA for one symbol."""
    if symbol not in SYMBOLS:
        raise ValueError("symbol must be VOO or QQQM")
    if monthly_budget <= 0:
        raise ValueError("DCA 月投入须大于零。")
    schedule_set = set(monthly_dates(prices, target_day))
    shares = 0.0
    daily_rows = []
    for dt, price in prices[symbol].items():
        price = float(price)
        deposit = monthly_budget if dt in schedule_set else 0.0
        if deposit:
            shares += deposit / price
        daily_rows.append({"Date": dt, "account_value": shares * price, "external_deposit": deposit})
    daily = pd.DataFrame(daily_rows).set_index("Date")
    metrics = _metrics_from_daily(daily, f"{symbol} 固定DCA")
    metrics["symbol"] = symbol
    metrics["monthly_budget_usd"] = monthly_budget
    return metrics, daily


def combine_two(
    first_daily: pd.DataFrame,
    second_daily: pd.DataFrame,
    label: str,
) -> tuple[dict, pd.DataFrame]:
    daily = pd.DataFrame(index=first_daily.index)
    daily["account_value"] = first_daily["account_value"] + second_daily["account_value"]
    daily["external_deposit"] = first_daily["external_deposit"] + second_daily["external_deposit"]
    metrics = _metrics_from_daily(daily, label)
    return metrics, daily


def combine_independent(voo_daily: pd.DataFrame, qqqm_daily: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    return combine_two(voo_daily, qqqm_daily, "独立VA合计")


def simulate_fixed_dca(
    prices: pd.DataFrame,
    monthly_total: float,
    weights: tuple[float, float] = (0.625, 0.375),
    target_day: int = 10,
) -> tuple[dict, pd.DataFrame]:
    """Compatibility helper: fixed DCA total split by weights."""
    if len(weights) != 2 or any(w < 0 for w in weights) or not np.isclose(sum(weights), 1.0):
        raise ValueError("DCA 权重须非负且合计 100%。")
    vm, vd = simulate_symbol_dca(prices, "VOO", monthly_total * weights[0], target_day)
    qm, qd = simulate_symbol_dca(prices, "QQQM", monthly_total * weights[1], target_day)
    combined, daily = combine_two(vd, qd, "固定DCA合计")
    combined["monthly_budget_usd"] = monthly_total
    combined["voo_weight"] = float(weights[0])
    combined["qqqm_weight"] = float(weights[1])
    return combined, daily


def run(
    prices: pd.DataFrame,
    voo_initial: float = 500.0,
    qqqm_initial: float = 300.0,
    voo_step: float = 500.0,
    qqqm_step: float = 300.0,
    target_day: int = 10,
    weights: tuple[float, float] = (0.625, 0.375),
):
    """Compatibility entry point for the independent VA ledger."""
    vm, vd, vl = simulate_symbol_va(prices, "VOO", voo_initial, voo_step, target_day)
    qm, qd, ql = simulate_symbol_va(prices, "QQQM", qqqm_initial, qqqm_step, target_day)
    combined_m, _ = combine_independent(vd, qd)
    ledger = vl.rename(columns={
        "target_value": "VOO_target", "pre_value": "VOO_pre_value", "gap": "VOO_gap",
        "buy": "VOO_buy", "post_value": "VOO_post_value"
    })[["Date", "month_no", "VOO_target", "VOO_pre_value", "VOO_gap", "VOO_buy", "VOO_post_value"]]
    qpart = ql.rename(columns={
        "target_value": "QQQM_target", "pre_value": "QQQM_pre_value", "gap": "QQQM_gap",
        "buy": "QQQM_buy", "post_value": "QQQM_post_value"
    })[["Date", "QQQM_target", "QQQM_pre_value", "QQQM_gap", "QQQM_buy", "QQQM_post_value"]]
    ledger = ledger.merge(qpart, on="Date", how="outer").sort_values("Date")
    ledger["raw_required"] = ledger["VOO_gap"].fillna(0) + ledger["QQQM_gap"].fillna(0)
    ledger["total_buy"] = ledger["VOO_buy"].fillna(0) + ledger["QQQM_buy"].fillna(0)
    summary = {
        "months": int(max(len(vl), len(ql))),
        "total_invested_usd": combined_m["contributed_usd"],
        "final_value_usd": combined_m["ending_usd"],
        "profit_usd": combined_m["profit_usd"],
        "VOO_invested_usd": vm["contributed_usd"],
        "QQQM_invested_usd": qm["contributed_usd"],
        "VOO_final_value_usd": vm["ending_usd"],
        "QQQM_final_value_usd": qm["ending_usd"],
    }
    return ledger, summary


def _fmt_money(x: float) -> str:
    return f"${float(x):,.2f}"


def _fmt_pct(x: float) -> str:
    return "数据不足" if pd.isna(x) else f"{float(x) * 100:+.2f}%"


def _img64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _make_charts(
    out: Path,
    independent_daily: pd.DataFrame,
    dca_daily: pd.DataFrame,
    voo_monthly: pd.DataFrame,
    qqqm_monthly: pd.DataFrame,
) -> dict[str, Path]:
    import matplotlib.pyplot as plt

    out.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    fig, ax = plt.subplots(figsize=(11, 5.5))
    ax.plot(independent_daily.index, independent_daily["account_value"], label="Independent VA")
    ax.plot(dca_daily.index, dca_daily["account_value"], label="Fixed DCA")
    ax.set_title("Independent VA vs Fixed DCA: Account Value")
    ax.set_ylabel("USD")
    ax.grid(alpha=0.22)
    ax.legend()
    fig.tight_layout()
    paths["account"] = out / "va_vs_dca_account_value.png"
    fig.savefig(paths["account"], dpi=150)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(11, 5.5))
    ax.plot(independent_daily.index, independent_daily["twr_drawdown"] * 100, label="Independent VA")
    ax.plot(dca_daily.index, dca_daily["twr_drawdown"] * 100, label="Fixed DCA")
    ax.set_title("Independent VA vs Fixed DCA: TWR Drawdown")
    ax.set_ylabel("%")
    ax.grid(alpha=0.22)
    ax.legend()
    fig.tight_layout()
    paths["drawdown"] = out / "va_vs_dca_drawdown.png"
    fig.savefig(paths["drawdown"], dpi=150)
    plt.close(fig)

    for symbol, monthly in (("VOO", voo_monthly), ("QQQM", qqqm_monthly)):
        fig, ax = plt.subplots(figsize=(11, 5.5))
        ax.plot(pd.to_datetime(monthly["Date"]), monthly["target_value"], label=f"{symbol} target")
        ax.plot(pd.to_datetime(monthly["Date"]), monthly["post_value"], label=f"{symbol} post-buy value")
        ax.set_title(f"{symbol} Independent VA: Target vs Actual")
        ax.set_ylabel("USD")
        ax.grid(alpha=0.22)
        ax.legend()
        fig.tight_layout()
        paths[symbol.lower()] = out / f"{symbol.lower()}_va_target_path.png"
        fig.savefig(paths[symbol.lower()], dpi=150)
        plt.close(fig)
    return paths


def _metric_row(m: dict) -> str:
    return "<tr>" + "".join([
        f"<td>{escape(m['strategy'])}</td>",
        f"<td>{_fmt_money(m['contributed_usd'])}</td>",
        f"<td>{_fmt_money(m['ending_usd'])}</td>",
        f"<td>{_fmt_money(m['profit_usd'])}</td>",
        f"<td>{_fmt_pct(m['money_weighted_xirr'])}</td>",
        f"<td>{_fmt_pct(m['time_weighted_cagr'])}</td>",
        f"<td>{_fmt_pct(m['max_twr_drawdown'])}</td>",
        f"<td>{m['num_contributions']}</td>",
        f"<td>{_fmt_money(m['avg_contribution_usd'])}</td>",
        f"<td>{_fmt_money(m['p95_contribution_usd'])}</td>",
        f"<td>{_fmt_money(m['max_contribution_usd'])}</td>",
    ]) + "</tr>"


def render_report(
    out: Path,
    prices: pd.DataFrame,
    metrics: dict[str, dict],
    daily: dict[str, pd.DataFrame],
    voo_monthly: pd.DataFrame,
    qqqm_monthly: pd.DataFrame,
    source: str,
    params: dict,
) -> Path:
    charts = _make_charts(out, daily["independent_va"], daily["fixed_dca"], voo_monthly, qqqm_monthly)
    start, end = prices.index[0].date(), prices.index[-1].date()

    def delta(a: dict, b: dict, key: str) -> float:
        av, bv = a[key], b[key]
        if pd.isna(av) or pd.isna(bv):
            return np.nan
        return float(av) - float(bv)

    html = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VOO / QQQM 独立 VA vs DCA 历史回测｜{end}</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#f4f7fb;color:#18243a;font:15px/1.7 -apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif}}
main{{max-width:1180px;margin:auto;padding:24px 16px 52px}}header{{background:#173f37;color:white;padding:28px;border-radius:17px}}h1{{margin:0 0 6px;font-size:28px}}h2{{font-size:20px;margin:0 0 12px}}section{{background:white;border:1px solid #e2e9e5;border-radius:15px;padding:22px;margin-top:17px}}.muted{{color:#667b72}}.lead{{font-size:16px}}.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}.card{{background:#f7faf8;border:1px solid #e5ece8;border-radius:12px;padding:15px}}.card b{{display:block;font-size:20px;margin-top:4px}}.table{{overflow:auto}}table{{border-collapse:collapse;width:100%;min-width:1050px}}th,td{{padding:9px 8px;border-bottom:1px solid #e9eeeb;text-align:right;white-space:nowrap}}th:first-child,td:first-child{{text-align:left}}th{{background:#eff5f1}}img{{width:100%;display:block;border:1px solid #e4ebe7;border-radius:11px;margin-top:14px}}.note{{border-left:4px solid #86ad98;padding:8px 12px;background:#f4f8f5}}@media(max-width:760px){{.cards{{grid-template-columns:1fr}}}}
</style></head><body><main>
<header><h1>VOO / QQQM 独立 VA vs 固定 DCA</h1><div>{start} 至 {end} · {len(prices):,} 个共同交易日</div></header>
<section><h2>01｜研究口径</h2><p class="lead">主策略只保留 <strong>VOO 独立 VA + QQQM 独立 VA</strong>；固定 DCA 仅作为 Benchmark。两只 ETF 在 VA 中各自计算目标、缺口和实际投入，互不抵消。</p>
<ul><li>VOO：首月目标 {_fmt_money(params['voo_initial'])}，之后每月目标 +{_fmt_money(params['voo_step'])}；DCA 对照每月固定 {_fmt_money(params['voo_step'])}。</li>
<li>QQQM：首月目标 {_fmt_money(params['qqqm_initial'])}，之后每月目标 +{_fmt_money(params['qqqm_step'])}；DCA 对照每月固定 {_fmt_money(params['qqqm_step'])}。</li>
<li>VA 只买不卖：当前价值高于目标时当月买入 0；低于目标时补足自身缺口。</li></ul></section>
<section><h2>02｜VOO：独立 VA vs DCA 研究基准</h2><div class="table"><table><thead><tr><th>策略</th><th>累计投入</th><th>期末资产</th><th>盈利</th><th>XIRR</th><th>TWR年化</th><th>最大回撤</th><th>投入次数</th><th>平均单次投入</th><th>P95单次投入</th><th>最大单次投入</th></tr></thead><tbody>{_metric_row(metrics['voo'])}{_metric_row(metrics['voo_dca'])}</tbody></table></div>
<div class="cards"><div class="card">VA - DCA 盈利差<b>{_fmt_money(delta(metrics['voo'], metrics['voo_dca'], 'profit_usd'))}</b></div><div class="card">XIRR差<b>{_fmt_pct(delta(metrics['voo'], metrics['voo_dca'], 'money_weighted_xirr'))}</b></div><div class="card">VA累计投入差<b>{_fmt_money(delta(metrics['voo'], metrics['voo_dca'], 'contributed_usd'))}</b></div></div><img alt="VOO VA target" src="data:image/png;base64,{_img64(charts['voo'])}"></section>
<section><h2>03｜QQQM：独立 VA vs DCA 研究基准</h2><div class="table"><table><thead><tr><th>策略</th><th>累计投入</th><th>期末资产</th><th>盈利</th><th>XIRR</th><th>TWR年化</th><th>最大回撤</th><th>投入次数</th><th>平均单次投入</th><th>P95单次投入</th><th>最大单次投入</th></tr></thead><tbody>{_metric_row(metrics['qqqm'])}{_metric_row(metrics['qqqm_dca'])}</tbody></table></div>
<div class="cards"><div class="card">VA - DCA 盈利差<b>{_fmt_money(delta(metrics['qqqm'], metrics['qqqm_dca'], 'profit_usd'))}</b></div><div class="card">XIRR差<b>{_fmt_pct(delta(metrics['qqqm'], metrics['qqqm_dca'], 'money_weighted_xirr'))}</b></div><div class="card">VA累计投入差<b>{_fmt_money(delta(metrics['qqqm'], metrics['qqqm_dca'], 'contributed_usd'))}</b></div></div><img alt="QQQM VA target" src="data:image/png;base64,{_img64(charts['qqqm'])}"></section>
<section><h2>04｜整体：独立 VA vs DCA 研究基准</h2><div class="table"><table><thead><tr><th>策略</th><th>累计投入</th><th>期末资产</th><th>盈利</th><th>XIRR</th><th>TWR年化</th><th>最大回撤</th><th>投入次数</th><th>平均单次投入</th><th>P95单次投入</th><th>最大单次投入</th></tr></thead><tbody>{_metric_row(metrics['independent_total'])}{_metric_row(metrics['fixed_dca'])}</tbody></table></div>
<p class="note">DCA 仅为研究基准，不是当前投资计划。VA 与 DCA 的累计投入通常不同，所以不能只比较期末资产。资金准备应重点看 P95 与最大单次投入；理论缺口不等于必须投入金额。</p>
<img alt="account values" src="data:image/png;base64,{_img64(charts['account'])}"><img alt="drawdowns" src="data:image/png;base64,{_img64(charts['drawdown'])}"></section>
<section><h2>05｜方法与边界</h2><ul><li>行情来源：{escape(source)}。</li><li>共同交易日复权收盘价；允许零碎份额；不计手续费、税费、价差和汇兑成本。</li><li>研究模型统一在每月 {params['target_day']} 日当天或之后的首个共同交易日一次性模拟买入；若最新月份尚未到该日，则该月不提前模拟投入。该日期只为保证回测可复现，与真实账户“每月 2 日锁定预算、月内择机分批”完全分开。</li><li>DCA 研究基准分别按 VOO {_fmt_money(params['voo_step'])}/月、QQQM {_fmt_money(params['qqqm_step'])}/月执行，合计 {_fmt_money(params['monthly_total'])}/月。</li><li>本策略属于只买不卖的目标价值补足法；历史回测用于研究规则，不代表未来收益。</li></ul></section>
</main></body></html>"""
    path = out / f"va_backtest_analysis_{end}.html"
    path.write_text(html, encoding="utf-8")
    return path


def run_full_study(
    prices: pd.DataFrame,
    out: Path,
    voo_initial: float = 500.0,
    qqqm_initial: float = 300.0,
    voo_step: float = 500.0,
    qqqm_step: float = 300.0,
    target_day: int = 10,
    weights: tuple[float, float] = (0.625, 0.375),
    source: str = "本地历史复权价格 CSV",
) -> dict:
    """Run the focused Independent VA vs DCA study.

    ``weights`` is retained in the signature for compatibility with the GUI, but
    DCA dollar amounts are taken directly from the two VA monthly steps so the
    benchmark is exactly VOO $500 + QQQM $300 by default.
    """
    out.mkdir(parents=True, exist_ok=True)
    vm, vd, vl = simulate_symbol_va(prices, "VOO", voo_initial, voo_step, target_day)
    qm, qd, ql = simulate_symbol_va(prices, "QQQM", qqqm_initial, qqqm_step, target_day)
    im, idaily = combine_independent(vd, qd)

    vdm, vdd = simulate_symbol_dca(prices, "VOO", voo_step, target_day)
    qdm, qdd = simulate_symbol_dca(prices, "QQQM", qqqm_step, target_day)
    dm, ddaily = combine_two(vdd, qdd, "固定DCA合计")
    dm["monthly_budget_usd"] = voo_step + qqqm_step

    metrics = {
        "voo": vm,
        "voo_dca": vdm,
        "qqqm": qm,
        "qqqm_dca": qdm,
        "independent_total": im,
        "fixed_dca": dm,
    }
    pd.DataFrame([vm, vdm, qm, qdm, im, dm]).to_csv(out / "va_strategy_summary.csv", index=False)
    vl.to_csv(out / "voo_va_monthly_ledger.csv", index=False)
    ql.to_csv(out / "qqqm_va_monthly_ledger.csv", index=False)
    for name, df in {
        "independent_va_daily": idaily,
        "fixed_dca_daily": ddaily,
        "voo_va_daily": vd,
        "qqqm_va_daily": qd,
        "voo_dca_daily": vdd,
        "qqqm_dca_daily": qdd,
    }.items():
        df.to_csv(out / f"{name}.csv", index_label="Date")

    # Remove obsolete mixed-VA artifacts from older builds so the output folder
    # reflects the narrowed research scope.
    for obsolete in (
        "mixed_va_daily.csv", "mixed_va_monthly_ledger.csv",
        "va_strategy_account_value.png", "va_strategy_drawdown.png",
    ):
        path = out / obsolete
        if path.exists():
            path.unlink()

    params = {
        "voo_initial": voo_initial, "qqqm_initial": qqqm_initial,
        "voo_step": voo_step, "qqqm_step": qqqm_step,
        "monthly_total": voo_step + qqqm_step, "target_day": target_day,
        "voo_weight": weights[0], "qqqm_weight": weights[1],
    }
    report = render_report(
        out, prices, metrics,
        {"independent_va": idaily, "fixed_dca": ddaily},
        vl, ql, source, params,
    )
    payload = {"parameters": params, "metrics": metrics, "report": str(report)}
    (out / "va_strategy_summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=True) + "\n", encoding="utf-8"
    )
    return {"report": report, "metrics": metrics, "parameters": params}


def main() -> None:
    ap = argparse.ArgumentParser(description="VOO/QQQM 独立 VA 与固定 DCA 历史回测")
    ap.add_argument("--prices", default="data/source_adjusted_close.csv")
    ap.add_argument("--out", default="va_backtest_results")
    ap.add_argument("--voo-initial", type=float, default=500)
    ap.add_argument("--qqqm-initial", type=float, default=300)
    ap.add_argument("--voo-step", type=float, default=500)
    ap.add_argument("--qqqm-step", type=float, default=300)
    ap.add_argument("--target-day", type=int, default=10)
    ap.add_argument("--voo-weight", type=float, default=0.625)
    ap.add_argument("--qqqm-weight", type=float, default=0.375)
    args = ap.parse_args()
    prices = load_prices(Path(args.prices))
    result = run_full_study(
        prices, Path(args.out), args.voo_initial, args.qqqm_initial,
        args.voo_step, args.qqqm_step, args.target_day,
        (args.voo_weight, args.qqqm_weight),
    )
    print(json.dumps({
        "report": str(result["report"]),
        "voo_va": result["metrics"]["voo"],
        "voo_dca": result["metrics"]["voo_dca"],
        "qqqm_va": result["metrics"]["qqqm"],
        "qqqm_dca": result["metrics"]["qqqm_dca"],
        "independent_va": result["metrics"]["independent_total"],
        "fixed_dca": result["metrics"]["fixed_dca"],
    }, ensure_ascii=False, indent=2, allow_nan=True))


if __name__ == "__main__":
    main()
