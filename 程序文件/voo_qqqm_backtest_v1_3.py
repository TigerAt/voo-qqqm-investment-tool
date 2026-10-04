#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VOO / QQQM 回测与离线中文 HTML 分析报告 V1.3。

首次运行（联网）：
    python voo_qqqm_backtest_v1_3.py --end 2026-09-18 --out results_v1_3
仅从已保存的行情重算并生成 HTML（不联网）：
    python voo_qqqm_backtest_v1_3.py --report-only --out results_v1_3
兼容 V1.1 已有结果目录：
    python voo_qqqm_backtest_v1_3.py --report-only --out results_v1_1

说明：仅使用 VOO 和 QQQM 实际交易历史；没有更早期 QQQ/指数代理数据。
"""
import argparse
import base64
from datetime import datetime
from html import escape
import math
from pathlib import Path
import sys

import numpy as np
import pandas as pd

WEIGHTS = [(1., 0.), (.8, .2), (.6, .4), (.4, .6), (0., 1.)]
INITIAL, MONTHLY = 0., 500.
TARGET_DAY = 10
LAUNCH = pd.Timestamp('2020-10-13')


def validate_prices(px, end=None):
    if not {'VOO', 'QQQM'}.issubset(px.columns):
        raise ValueError('行情必须包含 VOO 和 QQQM 两列。')
    px = px[['VOO', 'QQQM']].copy()
    px.index = pd.DatetimeIndex(pd.to_datetime(px.index)).tz_localize(None).normalize()
    px = px.loc[px.index >= LAUNCH].sort_index()
    if end is not None:
        px = px.loc[px.index <= pd.Timestamp(end)]
    if px.index.has_duplicates:
        raise ValueError('行情中存在重复交易日期，请先核对数据。')
    if px.isna().any().any() or not np.isfinite(px.to_numpy(dtype=float)).all():
        raise ValueError('行情有缺失值或非有限数值；不会自行填充价格。')
    if (px <= 0).any().any():
        raise ValueError('价格必须是正数。')
    if len(px) < 756:
        raise ValueError(f'有效共同交易日仅有 {len(px)}，需要至少 756 日。')
    return px


def read_prices(args):
    if bool(args.voo_csv) != bool(args.qqqm_csv):
        raise ValueError('CSV 模式需要同时提供 --voo-csv 和 --qqqm-csv。')
    if args.voo_csv:
        parts = []
        for symbol, filename in [('VOO', args.voo_csv), ('QQQM', args.qqqm_csv)]:
            df = pd.read_csv(filename)
            col = next((c for c in ['Adj Close', 'Adj_Close', 'adjclose', 'Adjusted Close'] if c in df), None)
            if col is None or 'Date' not in df:
                raise ValueError(f'{symbol} CSV 需要 Date 和 Adj Close 两列。')
            idx = pd.to_datetime(df['Date'], errors='raise')
            parts.append(pd.Series(pd.to_numeric(df[col], errors='raise').values, index=idx, name=symbol))
        px = pd.concat(parts, axis=1, join='inner')
        source = '用户提供的 VOO/QQQM 复权价格 CSV，尚未逐日独立核验原始来源'
    else:
        try:
            import yfinance as yf
        except ImportError as exc:
            raise RuntimeError('请先在虚拟环境安装 yfinance，或使用 --report-only 离线生成报告。') from exc
        parts = []
        for symbol in ['VOO', 'QQQM']:
            hist = yf.Ticker(symbol).history(
                start='2020-10-13', end=(pd.Timestamp(args.end) + pd.Timedelta(days=1)).strftime('%Y-%m-%d'),
                auto_adjust=True, actions=False,
            )
            if hist.empty:
                raise RuntimeError(f'{symbol} 行情下载失败；没有使用模拟数据。')
            parts.append(hist['Close'].rename(symbol))
        px = pd.concat(parts, axis=1, join='inner')
        source = 'Yahoo Finance，经 yfinance 获取的自动复权日收盘价；未逐日与基金公司/交易所独立交叉核验'
    px = validate_prices(px, end=args.end)
    if px.index.max() < pd.Timestamp(args.end) - pd.Timedelta(days=8):
        raise RuntimeError(f'行情可能过期：最后共同交易日 {px.index.max().date()}，指定结束日 {args.end}。')
    return px, source


def xirr(flows, terminal, dates, terminal_date):
    from scipy.optimize import brentq
    d0 = dates[0]
    years = np.array([(d-d0).days/365.25 for d in dates] + [(terminal_date-d0).days/365.25])
    amounts = np.array([-v for v in flows] + [terminal], dtype=float)
    def npv(r):
        return float(np.sum(amounts / (1+r)**years))
    try:
        if npv(-.9999)*npv(100.) > 0:
            return np.nan
        return float(brentq(npv, -.9999, 100., maxiter=500))
    except (ValueError, OverflowError):
        return np.nan


def drawdown_episode(nav):
    trough = nav.div(nav.cummax()).sub(1).idxmin()
    peak = nav.loc[:trough].idxmax()
    peak_value = nav.loc[peak]
    recovered = nav.loc[trough:].loc[lambda series: series >= peak_value]
    recovery = recovered.index[0] if len(recovered) else None
    return {
        'peak': peak.strftime('%Y-%m-%d'),
        'trough': trough.strftime('%Y-%m-%d'),
        'recovery': recovery.strftime('%Y-%m-%d') if recovery is not None else None,
        'days_underwater': (recovery-peak).days if recovery is not None else None,
    }


def monthly_contribution_indices(px, target_day=None):
    """Return one simulated monthly buy row on or after the chosen calendar day."""
    day = TARGET_DAY if target_day is None else target_day
    if type(day) is not int or not 1 <= day <= 28:
        raise ValueError('每月目标日必须是 1 至 28 的整数。')
    chosen = []
    groups = list(pd.Series(np.arange(len(px)), index=px.index).groupby(px.index.to_period('M')))
    for index, (_, locations) in enumerate(groups):
        eligible = locations[locations.index.day >= day]
        if len(eligible):
            chosen.append(int(eligible.iloc[0]))
        elif index < len(groups) - 1:
            chosen.append(int(locations.iloc[-1]))
    return set(chosen)


def simulate(px, weights, target_day=None):
    day = TARGET_DAY if target_day is None else target_day
    monthly_rows = monthly_contribution_indices(px, day)
    shares = np.zeros(2, dtype=float)
    account = np.zeros(len(px), dtype=float)
    deposits = np.zeros(len(px), dtype=float)
    actual_qqqm_weight = np.zeros(len(px), dtype=float)
    for i, (_, row) in enumerate(px.iterrows()):
        contribution = (INITIAL if i == 0 else 0.) + (MONTHLY if i in monthly_rows else 0.)
        deposits[i] = contribution
        shares += contribution * np.asarray(weights) / row.to_numpy()
        positions = shares * row.to_numpy()
        account[i] = positions.sum()
        actual_qqqm_weight[i] = positions[1]/account[i] if account[i] > 0 else 0.
    account = pd.Series(account, index=px.index, name='account_value')
    deposits = pd.Series(deposits, index=px.index, name='external_deposit')
    returns = pd.Series(0., index=px.index)
    previous_account = account.shift(1)
    funded = previous_account > 0
    returns.loc[funded] = (account.loc[funded] - deposits.loc[funded]) / previous_account.loc[funded] - 1.
    nav = (1.+returns).cumprod()
    dd = nav/nav.cummax()-1.
    period_years = (px.index[-1]-px.index[0]).days/365.25
    contributed = float(deposits.sum())
    positives = deposits[deposits > 0]
    label = f'{round(weights[0]*100):d}/{round(weights[1]*100):d}'
    metrics = {
        'portfolio': label,
        'start': str(px.index[0].date()),
        'end': str(px.index[-1].date()),
        'contributed_usd': round(contributed,2),
        'ending_usd': round(float(account.iloc[-1]), 2),
        'profit_usd': round(float(account.iloc[-1]-contributed), 2),
        'time_weighted_total_return': float(nav.iloc[-1]-1.),
        'time_weighted_cagr': float(nav.iloc[-1]**(1/period_years)-1.),
        'money_weighted_xirr': xirr(positives.to_numpy(), float(account.iloc[-1]), list(positives.index), px.index[-1]),
        'annualized_volatility': float(returns.iloc[1:].std(ddof=1)*np.sqrt(252)),
        'max_twr_drawdown': float(dd.min()),
        'max_account_value_drawdown_with_deposits': float((account/account.cummax()-1).min()),
        'actual_qqqm_weight_end': float(actual_qqqm_weight[-1]),
        'num_contributions': int((deposits > 0).sum()),
        'monthly_target_day': day,
        'max_dd_peak_date': drawdown_episode(nav)['peak'],
        'max_dd_trough_date': drawdown_episode(nav)['trough'],
        'max_dd_recovery_date': drawdown_episode(nav)['recovery'],
        'max_dd_days_underwater': drawdown_episode(nav)['days_underwater'],
    }
    for years in (1, 3, 5):
        rolling = (nav/nav.shift(252*years)-1).dropna() if len(nav)>252*years else pd.Series(dtype=float)
        metrics[f'rolling_{years}y_worst'] = float(rolling.min()) if len(rolling) else np.nan
        metrics[f'rolling_{years}y_best'] = float(rolling.max()) if len(rolling) else np.nan
        metrics[f'rolling_{years}y_samples'] = len(rolling)
    daily = pd.DataFrame({
        'account_value':account, 'external_deposit':deposits, 'twr_nav':nav,
        'twr_drawdown':dd, 'actual_qqqm_weight':actual_qqqm_weight,
    })
    return metrics, daily


def fmt_money(x):
    return f'${float(x):,.0f}'


def fmt_pct(x, decimals=2):
    return '数据不足' if pd.isna(x) else f'{float(x)*100:+.{decimals}f}%'


def fmt_abs_pct(x):
    return '数据不足' if pd.isna(x) else f'{float(x)*100:.2f}%'


def image_data(path):
    return base64.b64encode(path.read_bytes()).decode('ascii')


def render_report(out, px, summary, details, source):
    summary = summary.set_index('portfolio', drop=False)
    first, last = px.index[0], px.index[-1]
    duration = (last-first).days/365.25
    total = summary.iloc[0]['contributed_usd']
    contribution_count = summary.iloc[0]['num_contributions']
    v = summary.loc['100/0']; q = summary.loc['0/100']
    profit_delta = q['profit_usd']-v['profit_usd']
    dd_delta_pp = (abs(q['max_twr_drawdown']) - abs(v['max_twr_drawdown']))*100
    header = ['VOO/QQQM', '累计投入', '期末资产', '投资盈利', 'XIRR', 'TWR年化', '年化波动率', '最大回撤', '期末QQQM实际占比']
    rows = []
    for _, r in summary.iterrows():
        vals = [escape(str(r['portfolio'])), fmt_money(r['contributed_usd']), fmt_money(r['ending_usd']),
                fmt_money(r['profit_usd']), fmt_pct(r['money_weighted_xirr']), fmt_pct(r['time_weighted_cagr']),
                fmt_abs_pct(r['annualized_volatility']), fmt_pct(r['max_twr_drawdown']), fmt_abs_pct(r['actual_qqqm_weight_end'])]
        rows.append('<tr>' + ''.join(f'<td>{x}</td>' for x in vals) + '</tr>')
    rolling_rows = []
    for _, r in summary.iterrows():
        vals = [r['portfolio']]
        for k in (1,3,5):
            vals.extend([fmt_pct(r[f'rolling_{k}y_worst']), fmt_pct(r[f'rolling_{k}y_best'])])
        rolling_rows.append('<tr>' + ''.join(f'<td>{escape(str(x))}</td>' for x in vals) + '</tr>')
    episode_rows=[]
    for _, r in summary.iterrows():
        recovery = r['max_dd_recovery_date'] if isinstance(r['max_dd_recovery_date'], str) else '截至回测结束仍未恢复'
        episode_rows.append('<tr>' + ''.join(f'<td>{escape(str(x))}</td>' for x in [r['portfolio'],r['max_dd_peak_date'],r['max_dd_trough_date'],recovery]) + '</tr>')
    h = lambda s: escape(str(s))
    date_label = last.strftime('%Y-%m-%d')
    html = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VOO / QQQM 回测分析报告｜{date_label}</title>
<style>
:root{{--bg:#f4f7fc;--card:#fff;--ink:#16233b;--muted:#64748b;--line:#e4eaf2;--accent:#2464d2}}
*{{box-sizing:border-box}}body{{background:var(--bg);color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",Arial,sans-serif;line-height:1.7;margin:0}}
main{{max-width:1150px;margin:auto;padding:28px 16px 60px}}header{{padding:26px;background:linear-gradient(125deg,#17345f,#2464d2);color:white;border-radius:18px;margin-bottom:16px}}
h1{{font-size:27px;margin:0 0 6px}}header p{{opacity:.9;margin:0}}h2{{font-size:20px;margin:0 0 12px}}h3{{font-size:16px;margin:14px 0 8px}}section{{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px;margin:16px 0;box-shadow:0 2px 10px #10182808}}
.lead{{background:#eff5ff;padding:15px;border-left:4px solid var(--accent);border-radius:8px}}.warn{{background:#fff8e9;padding:14px;border-left:4px solid #e0a326;border-radius:8px}}
.muted,small{{color:var(--muted)}}.cards{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}}.kpi{{border:1px solid var(--line);padding:13px;border-radius:12px}}.kpi strong{{display:block;font-size:21px}}.kpi span{{color:var(--muted);font-size:12px}}
.tablewrap{{width:100%;overflow-x:auto}}table{{width:100%;border-collapse:collapse;font-size:13px;min-width:750px}}td,th{{border-bottom:1px solid var(--line);padding:10px 8px;text-align:right;white-space:nowrap}}th{{background:#eff4ff;color:#28405d}}td:first-child,th:first-child{{text-align:left;font-weight:600}}tr:hover td{{background:#f8fbff}}img.chart{{display:block;width:100%;height:auto;border:1px solid var(--line);border-radius:10px}}
ul{{padding-left:22px}}li{{margin:7px 0}}.footer{{font-size:12px;color:var(--muted)}}a{{color:#225ecc}}code{{background:#f1f4f9;padding:2px 4px;border-radius:4px}}
@media(max-width:680px){{main{{padding:10px 10px 32px}}header{{padding:18px}}h1{{font-size:22px}}section{{padding:15px}}.cards{{grid-template-columns:repeat(2,minmax(0,1fr))}}.kpi strong{{font-size:17px}}}}
@media print{{body{{background:#fff}}main{{max-width:none}}section{{box-shadow:none;break-inside:avoid}}header{{background:#17345f!important;-webkit-print-color-adjust:exact;print-color-adjust:exact}}}}
</style></head><body><main>
<header><h1>VOO / QQQM 历史回测分析报告</h1><p>本地自动生成｜数据区间：{first.date()} 至 {last.date()}｜共同交易日 {len(px):,} 天</p></header>
<section><h2>01｜本轮研究结论</h2>
<p class="lead"><strong>资金假设：首个共同交易日投入额外本金 {fmt_money(INITIAL)}；历史研究全区间每月固定投入 {fmt_money(MONTHLY)}，在每月 {TARGET_DAY} 日当天或之后的首个共同交易日执行。该恒定研究金额与真实账户按月份生效的定投计划分别管理；这是历史对比，不是交易信号。</strong> 在本次约 {duration:.1f} 年的共同历史区间内，提高 QQQM 配比伴随更高的期末账户价值，同时也伴随更深的历史回撤。选择实际配置前，应明确资金使用期限和个人能够承受的损失。</p>
<div class="cards"><div class="kpi"><span>每组累计投入</span><strong>{fmt_money(total)}</strong></div><div class="kpi"><span>发生资金流的交易日</span><strong>{int(contribution_count)} 次</strong></div><div class="kpi"><span>纯 VOO 期末资产</span><strong>{fmt_money(v['ending_usd'])}</strong></div><div class="kpi"><span>纯 QQQM 期末资产</span><strong>{fmt_money(q['ending_usd'])}</strong></div></div>
<p>同样的投入安排下，纯 QQQM 相比纯 VOO 的期末投资盈利多 <strong>{fmt_money(profit_delta)}</strong>；但最大回撤幅度也加深约 <strong>{dd_delta_pp:.2f} 个百分点</strong>。这仅描述本次历史数据，不是对未来收益的预测。</p>
<p class="warn">该模型以每月定投为核心，资金假设不等于实际交易指令。全部组合都含股票风险，回撤可能超过历史水平；本报告不自动推荐最终配置比例、未经验证的买入价格区间或实际下单。</p></section>
<section><h2>02｜五组配置：账户收益与风险</h2><p class="muted">配置比例为每笔新投入资金的买入比例，<b>并非每日强制再平衡的持仓比例</b>；实际持仓比例会随行情漂移。</p>
<div class="tablewrap"><table><thead><tr>{''.join(f'<th>{h(x)}</th>' for x in header)}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p><b>看表方法：</b>“期末资产－累计投入＝投资盈利”；XIRR按实际投入日期与最终资产日期计算；TWR年化排除外部追加本金影响；最大回撤按时间加权净值计算，避免把新增资金错当成收益。</p>
<img class="chart" alt="五组配置账户资产曲线" src="data:image/png;base64,{image_data(out/'account_value.png')}">
<p class="muted">上图账户资产包含持续追加本金，不可直接当作投资收益曲线。</p>
<img class="chart" alt="五组配置时间加权净值回撤曲线" src="data:image/png;base64,{image_data(out/'drawdown.png')}"></section>
<section><h2>03｜滚动持有收益（仅样本内历史）</h2>
<div class="tablewrap"><table><thead><tr><th>VOO/QQQM</th><th>1年最差</th><th>1年最好</th><th>3年最差</th><th>3年最好</th><th>5年最差</th><th>5年最好</th></tr></thead><tbody>{''.join(rolling_rows)}</tbody></table></div>
<p class="muted">按约每年252个交易日的滚动窗口计算，窗口高度重叠；五年窗口尤其少且起止日期相近，不能据此推出“持有五年必然盈利”。滚动结果为不考虑期间新增资金的投资净值变化。</p></section>
<section><h2>04｜最大回撤发生在何时？</h2><div class="tablewrap"><table><thead><tr><th>VOO/QQQM</th><th>回撤前净值高点</th><th>回撤低点</th><th>恢复该高点日期</th></tr></thead><tbody>{''.join(episode_rows)}</tbody></table></div>
<p class="muted">恢复日期取回测期间首次恢复此前净值高点的交易日；回撤风险不仅包括幅度，还包括恢复所需时间。</p></section>
<section><h2>05｜方法、数据源与限制</h2>
<ul><li><strong>价格来源：</strong>{h(source)}；输入为 VOO、QQQM 的历史复权收盘价。仅核对输入数据完整性与回测计算一致性，未逐日独立核验所有原始报价。</li>
<li><strong>起点：</strong>{first.date()}。QQQM 的真实交易历史不早于2020年10月；报告<b>未</b>将 QQQ、纳斯达克100指数或虚构价格混入 QQQM 真实回测。</li>
<li><strong>资金流：</strong>首个共同交易日独立投入初始额外资金 {fmt_money(INITIAL)}；每个自然月在 {TARGET_DAY} 日当天或之后的首个共同交易日投入恒定研究金额 {fmt_money(MONTHLY)}，若该月没有符合日期则使用当月最后一个共同交易日；本次累计投入 {fmt_money(total)}。真实计划的生效月份不会套入这项历史对照。买入允许零碎份额，不考虑资金尚未到账等执行限制。</li>
<li><strong>交易和分红：</strong>按当日复权收盘价模拟买入与持有；复权价格意在反映分红/拆分调整，不另行重复记入分红。不计买卖价差、手续费、税费、汇兑成本或跨境汇款费用。</li>
<li><strong>再平衡：</strong>新增资金按固定比例分配，无卖出再平衡。未纳入债券或持有现金的替代回报。</li>
<li><strong>指标：</strong>波动率以日收益率标准差×√252年化；XIRR使用每次投入日和真实回测结束日；滚动持有收益按252/756/1260个交易日计算。</li>
<li><strong>用途：</strong>本报告仅分析历史配置风险收益，不含当天的宏观新闻、估值、2Y/10Y、VIX、DXY或AI行业基本面，也不自动提供实盘买卖建议。</li></ul>
<p class="footer">报告生成时间（本机）：{h(datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z%z'))}。本报告为自包含 HTML：图表已内嵌，浏览器离线可打开。可保留CSV进行进一步审计。</p></section>
<section><h2>06｜下一轮研究</h2><p>下一轮在每月定投金额不变的基础上，比较不同基金配置、定投日期与风险，考虑闲置现金收益及跨境汇款成本，并通过样本外历史检验。不要只用某个历史区间的最高收益决定实际仓位。</p></section>
</main></body></html>'''
    filename = out / f'voo_qqqm_backtest_report_{date_label}.html'
    filename.write_text(html, encoding='utf-8')
    return filename


def create_charts(out, details):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(11,5))
    for name, df in details.items():
        ax.plot(df.index, df['account_value'], label=f'VOO/QQQM {name}')
    ax.set(title=f'Account value: ${INITIAL:,.0f} initial + ${MONTHLY:,.0f} monthly on/after day {TARGET_DAY}', ylabel='USD', xlabel='Date')
    ax.legend();fig.tight_layout();fig.savefig(out/'account_value.png', dpi=145);plt.close(fig)
    fig, ax = plt.subplots(figsize=(11,5))
    for name, df in details.items():
        ax.plot(df.index, df['twr_drawdown']*100, label=f'VOO/QQQM {name}')
    ax.set(title='Time-weighted drawdown (deposits excluded)', ylabel='%', xlabel='Date')
    ax.legend();fig.tight_layout();fig.savefig(out/'drawdown.png', dpi=145);plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--end', default='2026-09-18', help='联网下载行情的研究截止日（含）')
    parser.add_argument('--out', default='results', help='输出目录；离线模式可指定已有结果目录')
    parser.add_argument('--initial', type=float, default=0., help='首月额外一次性本金，默认0（首月仍正常投入每月定投金额）')
    parser.add_argument('--backtest-monthly-budget-usd', '--monthly',
                        dest='backtest_monthly_budget_usd', type=float, default=500.,
                        help='历史研究全区间的恒定月投入金额，默认500美元（--monthly 为兼容别名）')
    parser.add_argument('--target-day', type=int, default=10, help='每月模拟买入目标日（1–28），默认10')
    parser.add_argument('--voo-csv')
    parser.add_argument('--qqqm-csv')
    parser.add_argument('--report-only', action='store_true', help='读取 --out/source_adjusted_close.csv，离线复算并生成 HTML，无需联网')
    args = parser.parse_args()
    if args.initial < 0 or args.backtest_monthly_budget_usd <= 0:
        parser.error('--initial 不能为负，--backtest-monthly-budget-usd 必须为正')
    if not 1 <= args.target_day <= 28:
        parser.error('--target-day 必须是 1 至 28 的整数')
    global INITIAL, MONTHLY, TARGET_DAY
    INITIAL, MONTHLY, TARGET_DAY = (
        float(args.initial), float(args.backtest_monthly_budget_usd), args.target_day)
    out = Path(args.out)
    if args.report_only:
        cache = out/'source_adjusted_close.csv'
        if not cache.exists():
            raise FileNotFoundError(f'找不到本地历史行情：{cache}。请先运行一次联网回测。')
        px = pd.read_csv(cache, parse_dates=['Date'], index_col='Date')
        px = validate_prices(px)
        source = '本地既有 source_adjusted_close.csv，来源沿用原回测下载记录；本次没有联网刷新或独立验证行情'
    else:
        px, source = read_prices(args)
    out.mkdir(parents=True,exist_ok=True)
    # 将本次使用的资金参数写入结果目录，便于将来核对。
    (out/'model_parameters.txt').write_text(
        f'initial_extra_usd={INITIAL}\nbacktest_monthly_budget_usd={MONTHLY}\n'
        f'monthly_budget_mode=constant_for_full_history\n'
        f'first_month_total_usd={INITIAL+MONTHLY}\ntarget_day={TARGET_DAY}\n',
        encoding='utf-8')
    px.to_csv(out/'source_adjusted_close.csv',index_label='Date')
    summaries, details = [], {}
    for weights in WEIGHTS:
        metrics,daily = simulate(px,weights)
        summaries.append(metrics)
        label = metrics['portfolio']
        details[label] = daily
        daily.to_csv(out/f"portfolio_{label.replace('/', '_')}_daily.csv",index_label='Date')
    result = pd.DataFrame(summaries)
    result.to_csv(out/'five_portfolios_summary.csv',index=False)
    create_charts(out,details)
    html_file = render_report(out,px,result,details,source)
    print(f'回测日期：{px.index[0].date()} 至 {px.index[-1].date()}；共同交易日 {len(px)} 天')
    print(f'中文HTML报告：{html_file.resolve()}')
    print('报告自包含全部图表，直接双击即可离线查看。')

if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        print(f'回测/报告未成功完成：{exc}', file=sys.stderr)
        sys.exit(1)
