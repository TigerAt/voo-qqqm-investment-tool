"""On-demand U.S. market morning briefing for the local VOO/QQQM tool.

The briefing combines completed daily market data, recent source-attributed news,
and the user's existing plan.  It only writes its own cache and HTML report.  It
does not create transactions, change VA targets, or place orders.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import ProxyHandler, Request, build_opener
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import pandas as pd


SCHEMA_VERSION = 3
SYMBOLS = ("VOO", "QQQM")
SHANGHAI = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")
_UNSET = object()
USER_AGENT = "VOO-QQQM-local-morning-briefing/1.0"
MAX_FEED_BYTES = 2_000_000
NEWS_PRIMARY_HOURS = 72
NEWS_FALLBACK_HOURS = 168
TRUSTED_AGGREGATED_SOURCES = (
    "reuters", "associated press", "ap news", "bloomberg", "cnbc",
    "financial times", "wall street journal", "wsj", "barron", "marketwatch",
    "yahoo finance", "investor's business daily", "morningstar", "fortune",
    "business insider", "axios", "nikkei", "economist", "华尔街见闻", "财联社",
    "第一财经", "路透",
)
CHART_COLORS = {
    "VOO": "#2f7a57", "QQQM": "#c28a33", "标普 500": "#2f7a57",
    "纳斯达克 100": "#7759a6", "道琼斯": "#447aa1", "罗素 2000": "#c36d4d",
    "美元指数": "#447aa1", "WTI 原油": "#b06b3c", "黄金": "#c59b2d",
    "VIX": "#b04d52", "美国 10Y": "#6b5a9d",
}

MARKET_DEFINITIONS = (
    ("sp500", "^GSPC", "标普 500", "指数"),
    ("nasdaq100", "^NDX", "纳斯达克 100", "指数"),
    ("dow", "^DJI", "道琼斯", "指数"),
    ("russell2000", "^RUT", "罗素 2000", "指数"),
    ("dollar", "DX-Y.NYB", "美元指数", "跨资产"),
    ("wti", "CL=F", "WTI 原油", "跨资产"),
    ("gold", "GC=F", "黄金", "跨资产"),
)
CHART_ONLY_DEFINITIONS = (
    ("vix_history", "^VIX", "VIX", "图表辅助"),
    ("yield_history", "^TNX", "美国 10Y", "图表辅助"),
)

NEWS_FEEDS = (
    {
        "id": "fed",
        "name": "美联储",
        "url": "https://www.federalreserve.gov/feeds/press_all.xml",
        "category": "美联储与政策",
        "official": True,
    },
    {
        "id": "sec",
        "name": "美国证监会",
        "url": "https://www.sec.gov/news/pressreleases.rss",
        "category": "监管",
        "official": True,
    },
    {
        "id": "nyt_business",
        "name": "纽约时报商业",
        "url": "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
        "category": "市场与公司",
        "official": False,
    },
    {
        "id": "google_us_market",
        "name": "Google 新闻·美股",
        "url": (
            "https://news.google.com/rss/search?q="
            + quote("US stock market OR S&P 500 OR Nasdaq when:3d")
            + "&hl=en-US&gl=US&ceid=US:en"
        ),
        "category": "市场与公司",
        "official": False,
    },
    {
        "id": "google_macro_zh",
        "name": "Google 新闻·美国宏观",
        "url": (
            "https://news.google.com/rss/search?q="
            + quote("美联储 OR 美国通胀 OR 美债收益率 when:3d")
            + "&hl=zh-CN&gl=US&ceid=US:zh-Hans"
        ),
        "category": "美联储与政策",
        "official": False,
    },
)

# The user's shell can contain a stale localhost proxy.  These public feeds do
# not require it, so every worker gets a fresh direct opener.  Keeping the
# opener request-local also avoids sharing connection state across feed threads.
def _direct_open(request: Request, timeout: int):
    return build_opener(ProxyHandler({})).open(request, timeout=timeout)


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _number(value: object, digits: int = 4) -> str | None:
    number = _finite(value)
    if number is None:
        return None
    rendered = f"{number:.{digits}f}".rstrip("0").rstrip(".")
    return rendered if rendered not in ("", "-0") else "0"


def _atomic_text(path: Path, content: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=path.name + ".", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, value: dict) -> None:
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def empty_briefing(message: str | None = None) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "never_run",
        "headline": "尚未生成美股深度晨报",
        "summary": message or "点击“联网生成今日晨报”后，程序才会获取市场与新闻数据。",
        "stale": False,
    }


def load_briefing(
    path: Path, *,
    current_market_as_of: object = _UNSET,
    current_price_sha256: object = _UNSET,
    current_context_sha256: object = _UNSET,
    current_advice_signature: object = _UNSET,
    current_plan_signature: object = _UNSET,
) -> dict:
    """Load the last briefing without network access or file writes."""
    path = Path(path)
    if not path.is_file():
        return empty_briefing()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {
            **empty_briefing("上次晨报缓存无法读取，请重新生成。"),
            "status": "unavailable",
            "stale": True,
        }
    if (not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION
            or value.get("status") != "complete"):
        return {
            **empty_briefing("旧版或不完整的晨报缓存未沿用，请重新生成。"),
            "status": "unavailable",
            "stale": True,
        }
    result = dict(value)
    try:
        generated = datetime.fromisoformat(str(result.get("generated_at") or ""))
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=SHANGHAI)
        generated_day = generated.astimezone(SHANGHAI).date()
    except ValueError:
        generated_day = None
    stale_reasons: list[str] = []
    if generated_day != datetime.now(SHANGHAI).date():
        stale_reasons.append("这份晨报不是今天生成的")
    comparisons = (
        (current_market_as_of, result.get("market_as_of"), "本地 ETF 行情日期已变化"),
        (current_price_sha256, result.get("price_file_sha256"), "本地 ETF 行情内容已变化"),
        (current_context_sha256, result.get("context_file_sha256"), "VIX 或 10Y 市场环境数据已变化"),
        (current_advice_signature, result.get("advice_signature"), "策略辅助状态已变化"),
        (current_plan_signature, result.get("plan_signature"), "VA 预算、执行进度或账本现金已变化"),
    )
    for current, cached, reason in comparisons:
        if current is not _UNSET and current != cached:
            stale_reasons.append(reason)
    result["stale"] = bool(stale_reasons)
    if stale_reasons:
        result["warnings"] = list(dict.fromkeys([
            "；".join(stale_reasons) + "；请点击“联网生成今日晨报”取得最新资料。",
            *(result.get("warnings") or []),
        ]))
    return result


def _safe_url(value: object) -> str | None:
    url = str(value or "").strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    return url if parts.scheme in ("http", "https") and bool(parts.netloc) else None


def _plain_text(value: object, limit: int = 480) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rstrip()


def _parse_time(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(SHANGHAI)


def _node_text(node: ElementTree.Element, names: tuple[str, ...]) -> str:
    for child in node.iter():
        local = child.tag.rsplit("}", 1)[-1].lower()
        if local in names and child.text:
            return child.text.strip()
    return ""


def _entry_link(node: ElementTree.Element) -> str | None:
    for child in node.iter():
        if child.tag.rsplit("}", 1)[-1].lower() != "link":
            continue
        href = child.attrib.get("href") or child.text
        safe = _safe_url(href)
        if safe:
            return safe
    return None


def _classify_news(title: str, description: str, default: str) -> tuple[str, str, int]:
    text = f"{title} {description}".lower()
    if any(word in text for word in (
        "federal reserve", "fed ", "fomc", "powell", "interest rate",
        "美联储", "鲍威尔", "利率决议", "降息", "加息",
    )):
        return (
            "美联储与政策",
            "可能影响贴现率、成长股估值与风险偏好；结合 10Y 收益率及纳指反应核对。",
            4,
        )
    if any(word in text for word in (
        "inflation", "consumer price", "cpi", "ppi", "payroll", "employment",
        "jobs report", "unemployment", "通胀", "非农", "就业", "失业率",
    )):
        return (
            "通胀与就业",
            "可能改变利率预期与美债收益率；以官方数据和市场实际反应为准。",
            4,
        )
    if any(word in text for word in (
        "treasury", "bond yield", "yield curve", "美债", "收益率", "国债",
    )):
        return (
            "利率与流动性",
            "利率变化会影响估值和资金成本；重点比较 QQQM 与 VOO 的相对表现。",
            4,
        )
    if any(word in text for word in (
        "nvidia", "apple", "microsoft", "amazon", "alphabet", "google", "meta",
        "tesla", "semiconductor", "ai ", "芯片", "英伟达", "苹果", "微软",
        "亚马逊", "谷歌", "人工智能",
    )):
        return (
            "大型科技与 QQQM",
            "大型科技股在 QQQM 中权重较高，短期影响通常更直接；仍需核对指数整体走势。",
            3,
        )
    if any(word in text for word in (
        "sec ", "securities and exchange commission", "regulation", "antitrust",
        "tariff", "sanction", "监管", "反垄断", "关税", "制裁",
    )):
        return (
            "监管与政策风险",
            "先核对适用范围和正式文件，避免只凭标题推断对指数盈利的影响。",
            3,
        )
    if any(word in text for word in (
        "earnings", "profit", "revenue", "guidance", "forecast", "财报", "盈利", "营收",
    )):
        return (
            "盈利与公司",
            "盈利与指引会影响指数权重股预期；关注是否为广泛变化或单一公司事件。",
            3,
        )
    if any(word in text for word in (
        "s&p 500", "nasdaq", "dow jones", "stock market", "wall street", "美股", "标普", "纳指",
    )):
        return (
            "市场与公司",
            "用于理解隔夜风险偏好；新闻标题本身不改变 VA 目标，也不构成交易指令。",
            2,
        )
    impact = "作为背景资料阅读；先核对原文、发布时间与市场数据，再判断是否与 VOO/QQQM 有关。"
    return default, impact, 1


def _fetch_feed(definition: dict, now: datetime) -> tuple[list[dict], dict]:
    fetched_at = datetime.now(SHANGHAI).isoformat(timespec="seconds")
    status = {
        "id": definition["id"], "name": definition["name"],
        "url": definition["url"], "official": definition["official"],
        "status": "error", "fetched_at": fetched_at, "item_count": 0,
    }
    try:
        request = Request(
            definition["url"],
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.3"},
        )
        with _direct_open(request, timeout=10) as response:
            payload = response.read(MAX_FEED_BYTES + 1)
        if len(payload) > MAX_FEED_BYTES:
            raise ValueError("响应超过 2 MB 安全上限")
        root = ElementTree.fromstring(payload)
        nodes = [node for node in root.iter()
                 if node.tag.rsplit("}", 1)[-1].lower() in ("item", "entry")]
        items: list[dict] = []
        for node in nodes[:80]:
            title = _plain_text(_node_text(node, ("title",)), 240)
            link = _entry_link(node)
            published = _parse_time(_node_text(
                node, ("pubdate", "published", "updated", "date", "issued")
            ))
            if not title or not link or published is None:
                continue
            age_hours = (now - published).total_seconds() / 3600
            if age_hours < -6 or age_hours > NEWS_FALLBACK_HOURS:
                continue
            description = _plain_text(
                _node_text(node, ("description", "summary", "content", "encoded")), 480
            )
            publisher = _plain_text(_node_text(node, ("source",)), 80) or definition["name"]
            # Classify from the visible headline.  Feed descriptions often list
            # unrelated companies and can otherwise make a soft feature story
            # appear material to VOO or QQQM.
            category, impact, relevance = _classify_news(
                title, "", definition["category"]
            )
            lower_text = f"{title} {description}".lower()
            if definition["id"] == "fed":
                material = any(word in lower_text for word in (
                    "monetary policy", "fomc", "interest rate", "inflation", "employment",
                    "economic outlook", "financial stability", "liquidity", "bank capital",
                    "stress test",
                ))
                if not material:
                    continue
            if definition["id"] == "sec" and relevance < 3:
                continue
            if definition["id"].startswith("google_"):
                publisher_key = publisher.lower()
                if not any(source in publisher_key for source in TRUSTED_AGGREGATED_SOURCES):
                    continue
            if not definition["official"] and relevance < 2:
                continue
            items.append({
                "title": title,
                "url": link,
                "published_at": published.isoformat(timespec="minutes"),
                "published_display": published.strftime("%m-%d %H:%M"),
                "source": publisher,
                "feed_source": definition["name"],
                "official": bool(definition["official"]),
                "category": category,
                "description": description,
                "impact_note": impact,
                "relevance": relevance + (1 if definition["official"] else 0),
                "age_hours": round(max(0, age_hours), 1),
            })
        status.update(status="ok", item_count=len(items))
        return items, status
    except Exception as exc:
        status["error"] = _plain_text(exc, 180) or exc.__class__.__name__
        return [], status


def _news_key(item: dict) -> str:
    title = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(item.get("title") or "").lower())
    return hashlib.sha256(title.encode("utf-8")).hexdigest() if title else str(item.get("url"))


def fetch_news(now: datetime | None = None) -> tuple[list[dict], list[dict], int, list[str]]:
    now = now or datetime.now(SHANGHAI)
    collected: list[dict] = []
    statuses: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(6, len(NEWS_FEEDS))) as executor:
        futures = {executor.submit(_fetch_feed, definition, now): definition
                   for definition in NEWS_FEEDS}
        for future in as_completed(futures):
            items, status = future.result()
            collected.extend(items)
            statuses.append(status)
    statuses.sort(key=lambda item: next(
        index for index, source in enumerate(NEWS_FEEDS) if source["id"] == item["id"]
    ))
    deduped: dict[str, dict] = {}
    for item in collected:
        key = _news_key(item)
        existing = deduped.get(key)
        if existing is None or (item["official"], item["relevance"], item["published_at"]) > (
            existing["official"], existing["relevance"], existing["published_at"]
        ):
            deduped[key] = item
    primary = [item for item in deduped.values() if item["age_hours"] <= NEWS_PRIMARY_HOURS]
    window_hours = NEWS_PRIMARY_HOURS
    selected_pool = primary
    warnings: list[str] = []
    if len(primary) < 6:
        selected_pool = list(deduped.values())
        window_hours = NEWS_FALLBACK_HOURS
        warnings.append("近 72 小时可用新闻较少，新闻窗口已扩展到最近 7 天，并保留每条发布时间。")
    selected_pool.sort(
        key=lambda item: (item["relevance"], item["official"], item["published_at"]),
        reverse=True,
    )
    # Keep category diversity before filling the remaining slots.
    chosen: list[dict] = []
    seen_categories: set[str] = set()
    feed_counts: dict[str, int] = {}
    for item in selected_pool:
        feed = str(item.get("feed_source") or item.get("source") or "")
        if item["category"] not in seen_categories and feed_counts.get(feed, 0) < 2:
            chosen.append(item)
            seen_categories.add(item["category"])
            feed_counts[feed] = feed_counts.get(feed, 0) + 1
        if len(chosen) >= 5:
            break
    for item in selected_pool:
        feed = str(item.get("feed_source") or item.get("source") or "")
        if item not in chosen and feed_counts.get(feed, 0) < 3:
            chosen.append(item)
            feed_counts[feed] = feed_counts.get(feed, 0) + 1
        if len(chosen) >= 8:
            break
    chosen.sort(key=lambda item: item["published_at"], reverse=True)
    failures = [item for item in statuses if item["status"] != "ok"]
    if failures:
        warnings.append(
            "部分新闻源暂时不可用：" + "、".join(item["name"] for item in failures)
            + "；晨报已使用其余成功来源。"
        )
    if not chosen:
        warnings.append("本次未取得可验证的近期新闻；请直接查看来源页或稍后重试。")
    return chosen, statuses, window_hours, warnings


def _json_request(url: str, *, body: dict | None = None,
                  headers: dict[str, str] | None = None, timeout: int = 10) -> object:
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        **({"Content-Type": "application/json"} if body is not None else {}),
        **(headers or {}),
    }
    request = Request(url, data=payload, headers=request_headers)
    with _direct_open(request, timeout=timeout) as response:
        raw = response.read(MAX_FEED_BYTES + 1)
    if len(raw) > MAX_FEED_BYTES:
        raise ValueError("响应超过 2 MB 安全上限")
    return json.loads(raw.decode("utf-8"))


def _bls_points(series: dict) -> list[tuple[date, float, bool]]:
    points: list[tuple[date, float, bool]] = []
    for item in series.get("data") or []:
        period = str(item.get("period") or "")
        if not re.fullmatch(r"M(0[1-9]|1[0-2])", period):
            continue
        value = _finite(item.get("value"))
        if value is None:
            continue
        try:
            point_date = date(int(item["year"]), int(period[1:]), 1)
        except (KeyError, TypeError, ValueError):
            continue
        preliminary = any(
            str(note.get("code") or "").upper() == "P"
            for note in (item.get("footnotes") or []) if isinstance(note, dict)
        )
        points.append((point_date, value, preliminary))
    return sorted(points)


def fetch_bls_indicators(now: datetime | None = None) -> tuple[list[dict], dict, list[str]]:
    """Fetch a small batch from the official BLS public API without an API key."""
    now = now or datetime.now(SHANGHAI)
    url = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
    series_ids = (
        "CUSR0000SA0", "CUUR0000SA0", "CUSR0000SA0L1E",
        "LNS14000000", "CES0000000001", "CES0500000003",
    )
    status = {
        "id": "bls_api", "name": "美国劳工统计局 Public Data API",
        "url": "https://www.bls.gov/developers/", "official": True,
        "status": "error", "fetched_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "item_count": 0,
    }
    try:
        payload = _json_request(
            url,
            body={
                "seriesid": list(series_ids),
                "startyear": str(now.year - 1),
                "endyear": str(now.year),
            },
            timeout=10,
        )
        if not isinstance(payload, dict) or payload.get("status") != "REQUEST_SUCCEEDED":
            raise ValueError("BLS 返回的状态不是 REQUEST_SUCCEEDED")
        series_list = ((payload.get("Results") or {}).get("series") or [])
        series_map = {
            str(item.get("seriesID")): _bls_points(item)
            for item in series_list if isinstance(item, dict)
        }
        indicators: list[dict] = []

        def add_change(series_id: str, label: str, kind: str, *, year_over_year: bool = False,
                       level_unit: str | None = None) -> None:
            points = series_map.get(series_id) or []
            if not points:
                return
            latest_day, latest, preliminary = points[-1]
            comparison = None
            if year_over_year:
                comparison = next(
                    (value for day, value, _ in points
                     if day.year == latest_day.year - 1 and day.month == latest_day.month),
                    None,
                )
            elif len(points) >= 2:
                comparison = points[-2][1]
            if level_unit is not None:
                value = latest if kind == "level" else latest - comparison if comparison is not None else None
            else:
                value = ((latest / comparison - 1) * 100
                         if comparison not in (None, 0) else None)
            if value is None:
                return
            indicators.append({
                "series_id": series_id,
                "label": label,
                "value": _number(value, 2),
                "unit": level_unit or "%",
                "period": latest_day.strftime("%Y-%m"),
                "preliminary": preliminary,
                "source": "美国劳工统计局",
            })

        add_change("CUSR0000SA0", "CPI 环比", "change")
        add_change("CUUR0000SA0", "CPI 同比", "change", year_over_year=True)
        add_change("CUSR0000SA0L1E", "核心 CPI 环比", "change")
        add_change("LNS14000000", "失业率", "level", level_unit="%")
        add_change("CES0000000001", "非农就业月变动", "change", level_unit="千人")
        add_change("CES0500000003", "私营平均时薪环比", "change")
        status.update(status="ok", item_count=len(indicators))
        return indicators, status, []
    except Exception as exc:
        status["error"] = _plain_text(exc, 180) or exc.__class__.__name__
        return [], status, ["BLS 官方宏观指标暂时不可用；未用媒体估计值替代。"]


def _next_calendar_day(now: datetime) -> date:
    us_now = now.astimezone(NEW_YORK)
    candidate = us_now.date()
    if us_now.weekday() >= 5 or us_now.hour >= 16:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _market_cap_number(value: object) -> float:
    raw = re.sub(r"[^0-9.]", "", str(value or ""))
    number = _finite(raw)
    if number is None:
        return 0.0
    text = str(value or "").upper()
    if "T" in text:
        number *= 1_000_000_000_000
    elif "B" in text:
        number *= 1_000_000_000
    elif "M" in text:
        number *= 1_000_000
    return number


def fetch_nasdaq_calendar(now: datetime | None = None) -> tuple[dict, list[dict], list[str]]:
    """Fetch an optional next-session calendar from Nasdaq's public web endpoint."""
    now = now or datetime.now(SHANGHAI)
    target = _next_calendar_day(now)
    base = "https://api.nasdaq.com/api/calendar"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
        ),
        "Origin": "https://www.nasdaq.com",
        "Referer": "https://www.nasdaq.com/market-activity/",
    }
    statuses: list[dict] = []
    warnings: list[str] = []
    calendar = {"date": target.isoformat(), "economic_events": [], "earnings": []}
    definitions = (
        ("economic", "美国经济事件日历", f"{base}/economicevents?date={target.isoformat()}"),
        ("earnings", "美股财报日历", f"{base}/earnings?date={target.isoformat()}"),
    )
    for key, name, url in definitions:
        status = {
            "id": f"nasdaq_{key}", "name": f"Nasdaq {name}",
            "url": "https://www.nasdaq.com/market-activity/" + ("economic-calendar" if key == "economic" else "earnings"),
            "official": False, "status": "error",
            "fetched_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
            "item_count": 0,
        }
        try:
            payload = _json_request(url, headers=headers, timeout=10)
            rows = (((payload or {}).get("data") or {}).get("rows") or [])
            if not isinstance(rows, list):
                raise ValueError("日历 rows 格式不正确")
            if key == "economic":
                events = []
                for item in rows:
                    if not isinstance(item, dict):
                        continue
                    country = _plain_text(item.get("country"), 80)
                    country_key = country.lower().strip()
                    if country and not (
                        country_key in ("us", "usa", "u.s.")
                        or any(word in country_key for word in ("united states", "u.s", "美国"))
                    ):
                        continue
                    name_text = _plain_text(item.get("eventName"), 180)
                    if not name_text:
                        continue
                    events.append({
                        "time": _plain_text(item.get("gmt"), 40) or "时间待定",
                        "name": name_text,
                        "consensus": _plain_text(item.get("consensus"), 80) or None,
                        "previous": _plain_text(item.get("previous"), 80) or None,
                        "actual": _plain_text(item.get("actual"), 80) or None,
                        "description": _plain_text(item.get("description"), 280),
                    })
                calendar["economic_events"] = events[:10]
                count = len(calendar["economic_events"])
            else:
                earnings = []
                for item in rows:
                    if not isinstance(item, dict) or not item.get("symbol"):
                        continue
                    time_label = {
                        "time-before-hours": "盘前",
                        "time-after-hours": "盘后",
                        "time-not-supplied": "时间未公布",
                    }.get(str(item.get("time") or ""), _plain_text(item.get("time"), 50) or "时间待定")
                    earnings.append({
                        "symbol": _plain_text(item.get("symbol"), 16),
                        "name": _plain_text(item.get("name"), 120),
                        "time": time_label,
                        "eps_forecast": _plain_text(item.get("epsForecast"), 50) or None,
                        "market_cap": _plain_text(item.get("marketCap"), 50) or None,
                        "market_cap_sort": _market_cap_number(item.get("marketCap")),
                    })
                earnings.sort(key=lambda item: item["market_cap_sort"], reverse=True)
                for item in earnings:
                    item.pop("market_cap_sort", None)
                calendar["earnings"] = earnings[:8]
                count = len(calendar["earnings"])
            status.update(status="ok", item_count=count)
        except Exception as exc:
            status["error"] = _plain_text(exc, 180) or exc.__class__.__name__
            warnings.append(f"{name}暂时不可用；日历为辅助资料，未影响行情和账户结论。")
        statuses.append(status)
    if target.weekday() < 5:
        calendar["note"] = "日历日期按纽约工作日推算；美股节假日仍以 Nasdaq 或 Schwab 日历为准。"
    return calendar, statuses, warnings


def _series_metrics(series: pd.Series, *, price_digits: int = 4) -> dict | None:
    values = pd.to_numeric(series, errors="coerce").dropna()
    values = values.loc[values.map(lambda item: math.isfinite(float(item)) and float(item) > 0)]
    if len(values) < 2:
        return None
    last = float(values.iloc[-1])

    def change(periods: int) -> str | None:
        if len(values) <= periods:
            return None
        return _number((last / float(values.iloc[-periods - 1]) - 1) * 100, 3)

    high = float(values.tail(252).max())
    ma20 = float(values.tail(20).mean()) if len(values) >= 20 else None
    ma50 = float(values.tail(50).mean()) if len(values) >= 50 else None
    ma200 = float(values.tail(200).mean()) if len(values) >= 200 else None
    return {
        "date": pd.Timestamp(values.index[-1]).date().isoformat(),
        "close": _number(last, price_digits),
        "change_1d_pct": change(1),
        "change_5d_pct": change(5),
        "change_1m_pct": change(21),
        "drawdown_52w_pct": _number(max(0.0, (high - last) / high * 100), 3),
        "ma20": _number(ma20, price_digits),
        "ma50": _number(ma50, price_digits),
        "ma200": _number(ma200, price_digits),
        "ma20_position": None if ma20 is None else ("above" if last >= ma20 else "below"),
        "ma50_position": None if ma50 is None else ("above" if last >= ma50 else "below"),
        "ma200_position": None if ma200 is None else ("above" if last >= ma200 else "below"),
        "observations": len(values),
    }


def _history_points(series: pd.Series, *, sessions: int = 126,
                    max_points: int = 72) -> list[dict]:
    values = pd.to_numeric(series, errors="coerce").dropna()
    values = values.loc[values.map(lambda item: math.isfinite(float(item)) and float(item) > 0)]
    values = values[~values.index.duplicated(keep="last")].sort_index()
    if len(values) < 2:
        return []
    rolling_high = values.rolling(252, min_periods=1).max()
    tail = values.tail(sessions)
    base = float(tail.iloc[0])
    if len(tail) <= max_points:
        positions = list(range(len(tail)))
    else:
        positions = sorted({
            round(index * (len(tail) - 1) / (max_points - 1))
            for index in range(max_points)
        })
    result = []
    for position in positions:
        stamp = tail.index[position]
        value = float(tail.iloc[position])
        high = float(rolling_high.loc[stamp])
        result.append({
            "date": pd.Timestamp(stamp).date().isoformat(),
            "close": _number(value, 4),
            "return_pct": _number((value / base - 1) * 100, 3),
            "drawdown_pct": _number((value / high - 1) * 100, 3),
        })
    return result


def _chart_spec(chart_id: str, title: str, subtitle: str, items: list[dict],
                value_key: str, *, unit: str = "%", zero_line: bool = True,
                thresholds: list[dict] | None = None) -> dict | None:
    series = []
    for item in items:
        points = [
            {"date": point.get("date"), "value": point.get(value_key)}
            for point in item.get("history") or []
            if point.get("date") and _finite(point.get(value_key)) is not None
        ]
        if len(points) < 2:
            continue
        name = str(item.get("label") or item.get("symbol") or item.get("ticker") or "市场")
        series.append({
            "name": name,
            "color": CHART_COLORS.get(name, "#5d7f73"),
            "points": points,
        })
    if not series:
        return None
    return {
        "id": chart_id,
        "title": title,
        "subtitle": subtitle,
        "unit": unit,
        "zero_line": zero_line,
        "thresholds": thresholds or [],
        "series": series,
    }


def _market_charts(core: list[dict], broad: list[dict]) -> list[dict]:
    broad_by_key = {item.get("key"): item for item in broad}
    specs = [
        _chart_spec(
            "etf-performance", "VOO / QQQM 近 6 个月累计表现",
            "各自以图表起点为 0%，用于比较相对强弱。", core, "return_pct",
        ),
        _chart_spec(
            "etf-drawdown", "VOO / QQQM 距 252 日高点回撤",
            "数值越低代表距离各自近 252 日最高收盘价越远。", core, "drawdown_pct",
        ),
        _chart_spec(
            "index-performance", "主要美股指数近 6 个月走势",
            "标普 500、纳斯达克 100、道琼斯和罗素 2000，起点统一为 0%。",
            [broad_by_key[key] for key in ("sp500", "nasdaq100", "dow", "russell2000")
             if key in broad_by_key], "return_pct",
        ),
        _chart_spec(
            "cross-asset", "美元、原油与黄金近 6 个月走势",
            "跨资产变化用于辅助观察风险偏好和通胀环境，起点统一为 0%。",
            [broad_by_key[key] for key in ("dollar", "wti", "gold") if key in broad_by_key],
            "return_pct",
        ),
        _chart_spec(
            "vix-history", "VIX 近 6 个月走势",
            "20 与 30 为波动环境参考线，不是自动交易阈值。",
            [broad_by_key["vix_history"]] if "vix_history" in broad_by_key else [],
            "close", unit="", zero_line=False,
            thresholds=[{"value": 20, "label": "20"}, {"value": 30, "label": "30"}],
        ),
        _chart_spec(
            "yield-history", "美国 10Y 收益率近 6 个月走势",
            "单独纵轴显示收益率水平，用于观察估值压力变化。",
            [broad_by_key["yield_history"]] if "yield_history" in broad_by_key else [],
            "close", unit="%", zero_line=False,
        ),
    ]
    return [item for item in specs if item is not None]


def read_core_market(price_path: Path) -> tuple[list[dict], str]:
    try:
        from market_prices import resolve
        price_path = resolve(price_path)
        frame = pd.read_csv(price_path, parse_dates=["Date"], index_col="Date")
    except (OSError, ValueError, KeyError) as exc:
        raise ValueError(f"无法读取 VOO/QQQM 行情：{exc}") from exc
    if not set(SYMBOLS).issubset(frame.columns):
        raise ValueError("晨报行情必须同时包含 VOO 和 QQQM。")
    rows = []
    for symbol in SYMBOLS:
        metrics = _series_metrics(frame[symbol])
        if metrics is None or metrics["observations"] < 252:
            raise ValueError(f"{symbol} 有效历史行情不足，无法生成晨报。")
        rows.append({"key": symbol.lower(), "symbol": symbol, "label": symbol,
                     "group": "核心 ETF", "history": _history_points(frame[symbol]),
                     **metrics})
    as_of = min(item["date"] for item in rows)
    return rows, as_of


def _extract_close(data: pd.DataFrame, ticker: str) -> pd.Series | None:
    if data is None or data.empty:
        return None
    try:
        if isinstance(data.columns, pd.MultiIndex):
            first = set(map(str, data.columns.get_level_values(0)))
            if ticker in first:
                part = data[ticker]
                return part["Close"] if "Close" in part else None
            if "Close" in first:
                part = data["Close"]
                return part[ticker] if ticker in part else None
        if "Close" in data.columns and len(MARKET_DEFINITIONS) == 1:
            return data["Close"]
    except (KeyError, TypeError, IndexError):
        return None
    return None


def fetch_broad_market(as_of: str) -> tuple[list[dict], list[str]]:
    try:
        import yfinance as yf
    except ImportError as exc:
        return [], [f"缺少 yfinance，未取得外围市场快照：{exc}"]
    from market_network import direct_yfinance_session
    definitions = (*MARKET_DEFINITIONS, *CHART_ONLY_DEFINITIONS)
    tickers = [item[1] for item in definitions]
    try:
        session = direct_yfinance_session()
        data = yf.download(
            tickers=tickers, period="14mo", interval="1d", auto_adjust=False,
            actions=False, group_by="ticker", threads=True, progress=False,
            timeout=20, session=session,
        )
    except Exception as exc:
        return [], [f"外围市场快照更新失败：{_plain_text(exc, 180)}"]
    cutoff = pd.Timestamp(as_of)
    rows: list[dict] = []
    warnings: list[str] = []
    chart_only_keys = {item[0] for item in CHART_ONLY_DEFINITIONS}
    for key, ticker, label, group in definitions:
        series = _extract_close(data, ticker)
        if series is not None:
            series = series.copy()
            series.index = pd.DatetimeIndex(series.index).tz_localize(None).normalize()
            series = series.loc[series.index <= cutoff]
        metrics = _series_metrics(series) if series is not None else None
        if metrics is None:
            warnings.append(f"{label} 暂无有效快照。")
            continue
        rows.append({"key": key, "ticker": ticker, "label": label,
                     "group": group, "chart_only": key in chart_only_keys,
                     "history": _history_points(series), **metrics})
    if len(rows) < 4:
        warnings.append("外围市场可用项目较少，市场宽度结论仅供参考。")
    return rows, warnings


def read_macro(path: Path) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [], ["VIX 与 10Y 市场环境缓存无法读取。"]
    result: list[dict] = []
    definitions = (
        ("vix", "VIX", "波动率", False),
        ("yield_10y_pct", "美国 10Y 收益率", "利率", True),
    )
    for key, label, group, is_yield in definitions:
        item = (payload.get("values") or {}).get(key)
        if not isinstance(item, dict) or _finite(item.get("value")) is None or not item.get("date"):
            warnings.append(f"{label} 暂无有效数据。")
            continue
        change_1d = _finite(item.get("change_1d"))
        change_5d = _finite(item.get("change_5d"))
        result.append({
            "key": key,
            "ticker": item.get("ticker"),
            "label": label,
            "group": group,
            "date": str(item["date"]),
            "close": _number(item["value"], 4),
            "change_1d": _number(change_1d, 4),
            "change_5d": _number(change_5d, 4),
            "change_1d_display": (
                f"{_number(change_1d * 100, 1)} bp" if is_yield and change_1d is not None
                else (f"{_number(change_1d, 2)} 点" if change_1d is not None else None)
            ),
            "change_5d_display": (
                f"{_number(change_5d * 100, 1)} bp" if is_yield and change_5d is not None
                else (f"{_number(change_5d, 2)} 点" if change_5d is not None else None)
            ),
            "unit": "%" if is_yield else "",
            "source": item.get("source") or "Yahoo Finance（yfinance）",
        })
    warnings.extend(str(item) for item in payload.get("warnings", []) if item)
    return result, warnings


def _market_digest(core: list[dict], broad: list[dict], macro: list[dict], plan: dict) -> dict:
    by_key = {item["key"]: item for item in [*broad, *macro]}
    index_rows = [by_key[key] for key in ("sp500", "nasdaq100", "dow", "russell2000")
                  if key in by_key and _finite(by_key[key].get("change_1d_pct")) is not None]
    changes = [_finite(item["change_1d_pct"]) for item in index_rows]
    clean_changes = [item for item in changes if item is not None]
    average = sum(clean_changes) / len(clean_changes) if clean_changes else None
    positives = sum(1 for item in clean_changes if item > 0)
    if average is None:
        tone = "市场宽度资料不足"
        tone_status = "warn"
    elif average >= 0.6 or (positives >= 3 and average > 0.15):
        tone = "隔夜风险偏好偏强"
        tone_status = "pass"
    elif average <= -0.6 or (positives <= 1 and average < -0.15):
        tone = "隔夜市场承压"
        tone_status = "warn"
    else:
        tone = "隔夜市场分化"
        tone_status = "neutral"

    bullets: list[dict] = []
    if clean_changes:
        bullets.append({
            "status": tone_status,
            "title": "主要指数",
            "detail": f"{len(clean_changes)} 个主要指数中 {positives} 个上涨，平均 1 日变动 {_number(average, 2)}%。",
        })
    vix = by_key.get("vix")
    if vix and _finite(vix.get("close")) is not None:
        value = _finite(vix["close"])
        label = "高波动" if value >= 30 else "波动偏高" if value >= 20 else "波动较低" if value < 15 else "波动中性"
        bullets.append({
            "status": "warn" if value >= 20 else "neutral",
            "title": "VIX",
            "detail": f"{_number(value, 2)}，处于{label}区间；1 日变化 {vix.get('change_1d_display') or '—'}。",
        })
    yield10 = by_key.get("yield_10y_pct")
    if yield10:
        bullets.append({
            "status": "neutral",
            "title": "美国 10Y",
            "detail": f"{yield10.get('close') or '—'}%，1 日变化 {yield10.get('change_1d_display') or '—'}；用于观察估值压力。",
        })
    core_by_symbol = {item["symbol"]: item for item in core}
    voo, qqqm = core_by_symbol.get("VOO"), core_by_symbol.get("QQQM")
    if voo and qqqm:
        relative = (_finite(qqqm.get("change_1d_pct")) or 0) - (_finite(voo.get("change_1d_pct")) or 0)
        leader = "QQQM" if relative > 0.05 else "VOO" if relative < -0.05 else "两者接近"
        def ma200_text(item: dict) -> str:
            position = item.get("ma200_position")
            return "高于 MA200" if position == "above" else "低于 MA200" if position == "below" else "MA200 资料不足"
        bullets.append({
            "status": "neutral",
            "title": "VOO / QQQM",
            "detail": (
                f"VOO 1 日 {voo.get('change_1d_pct') or '—'}%、距 252 日高点 {voo.get('drawdown_52w_pct') or '—'}%、{ma200_text(voo)}；"
                f"QQQM 1 日 {qqqm.get('change_1d_pct') or '—'}%、距 252 日高点 {qqqm.get('drawdown_52w_pct') or '—'}%、{ma200_text(qqqm)}；"
                f"当日相对表现为 {leader}。"
            ),
        })
    locked = bool(plan.get("budget_locked"))
    amount = max(0.0, _finite(
        plan.get("remaining_budget_usd") if locked else plan.get("live_gap_total_usd")
    ) or 0)
    month = str(plan.get("month") or "当前月份")
    status = "剩余预算" if locked else "待锁定预估缺口"
    headline = f"{tone}｜{month} VA {status} ${amount:,.2f}"
    return {"tone": tone, "tone_status": tone_status, "headline": headline, "bullets": bullets}


def _plan_notes(plan: dict, advice: dict | None = None, extra: dict | None = None) -> dict:
    """Describe the single active independent-VA plan for the morning briefing."""
    symbols = plan.get("symbols") if isinstance(plan.get("symbols"), dict) else {}
    month = str(plan.get("month") or "—")
    preview = bool(plan.get("preview"))
    locked = bool(plan.get("budget_locked"))
    locked_budget = max(0.0, _finite(plan.get("locked_budget_usd")) or 0)
    actual_buy = max(0.0, _finite(plan.get("actual_buy_usd")) or 0)
    suggested = max(0.0, _finite(
        plan.get("remaining_budget_usd") if locked else plan.get("live_gap_total_usd")
    ) or 0)
    cash = max(0.0, _finite(plan.get("available_cash_usd")) or 0)

    details = []
    for symbol in SYMBOLS:
        item = symbols.get(symbol) if isinstance(symbols.get(symbol), dict) else {}
        target = max(0.0, _finite(item.get("target_value_usd")) or 0)
        if locked:
            value = max(0.0, _finite(item.get("reference_market_value_usd")) or 0)
            budget = max(0.0, _finite(item.get("locked_budget_usd")) or 0)
            bought = max(0.0, _finite(item.get("actual_buy_usd")) or 0)
            remaining = max(0.0, _finite(item.get("remaining_usd")) or 0)
            details.append(
                f"{symbol} 目标 ${target:,.2f}、预算日参考市值 ${value:,.2f}、"
                f"锁定预算 ${budget:,.2f}、已买 ${bought:,.2f}、剩余 ${remaining:,.2f}"
            )
        else:
            value = max(0.0, _finite(item.get("current_market_value_usd")) or 0)
            gap = max(0.0, _finite(item.get("live_gap_usd") or item.get("suggested_buy_usd")) or 0)
            details.append(f"{symbol} 目标 ${target:,.2f}、当前参考市值 ${value:,.2f}、预估缺口 ${gap:,.2f}")
    va_title = (
        f"{month} 独立 VA 预算已锁定"
        if locked else f"{month} 独立 VA 预算待锁定"
    )
    va_detail = (
        "；".join(details) +
        (f"；合计锁定预算 ${locked_budget:,.2f}、已买 ${actual_buy:,.2f}、剩余 ${suggested:,.2f}。"
         if locked else f"；合计预估缺口 ${suggested:,.2f}，锁定前会随行情变化。")
    )

    if suggested <= 0.005:
        funding_status = "covered"
        funding_title = "本月已无剩余预算" if locked else "当前两只 ETF 均已达到目标"
        funding_detail = (
            "本月锁定预算已经执行完毕或无需买入。"
            if locked else "当前预估缺口为 $0.00；只买不卖规则不会因高于目标而卖出。"
        )
    elif cash + 0.005 >= suggested:
        funding_status = "ready"
        funding_title = "账本现金可覆盖本月剩余预算" if locked else "账本现金可覆盖当前预估缺口"
        funding_detail = (
            f"账本可用现金 ${cash:,.2f}，{'剩余预算' if locked else '预估缺口'} ${suggested:,.2f}。"
            "实际成交仍以 Schwab 可用现金、价格和股数为准。"
        )
    else:
        funding_status = "shortfall"
        funding_title = "本月剩余预算高于账本现金" if locked else "当前预估缺口高于账本现金"
        funding_detail = (
            f"账本可用现金 ${cash:,.2f}，{'剩余预算' if locked else '预估缺口'} ${suggested:,.2f}，"
            f"相差 ${suggested - cash:,.2f}。不要求透支执行。"
        )
    return {
        "va": {"status": "locked" if locked else "preview" if preview else "active", "title": va_title, "detail": va_detail},
        "funding": {"status": funding_status, "title": funding_title, "detail": funding_detail},
    }

def _session_note(now: datetime, market_as_of: str) -> str:
    us_now = now.astimezone(NEW_YORK)
    weekday = us_now.weekday()
    if weekday >= 5:
        return f"纽约时间为周末；最近完整市场数据截至 {market_as_of}，下次开市仍请以 Schwab 日历为准。"
    if us_now.hour < 9 or (us_now.hour == 9 and us_now.minute < 30):
        return f"纽约常规交易时段尚未开始；晨报使用截至 {market_as_of} 的完整日线。"
    if us_now.hour < 16:
        return f"纽约常规交易时段进行中；晨报仍只使用截至 {market_as_of} 的完整日线，不把盘中价格当收盘数据。"
    return f"纽约常规交易时段已结束；已校验的完整日线截至 {market_as_of}。"


def _today_watch(digest: dict, macro: list[dict], news: list[dict], calendar: dict,
                 market_as_of: str, now: datetime) -> list[dict]:
    items = [{"title": "交易时段", "detail": _session_note(now, market_as_of)}]
    macro_by_key = {item["key"]: item for item in macro}
    vix = macro_by_key.get("vix")
    yield10 = macro_by_key.get("yield_10y_pct")
    if vix:
        items.append({
            "title": "波动率",
            "detail": f"VIX {vix.get('close') or '—'}；若盘前或开盘后显著跳升，先核对新闻来源和买卖价差。",
        })
    if yield10:
        items.append({
            "title": "利率",
            "detail": f"10Y {yield10.get('close') or '—'}%；关注收益率变化是否与 QQQM 相对表现一致。",
        })
    for item in (calendar.get("economic_events") or [])[:3]:
        detail = f"{item.get('time') or '时间待定'}"
        if item.get("consensus"):
            detail += f" · 市场预期 {item['consensus']}"
        if item.get("previous"):
            detail += f" · 前值 {item['previous']}"
        items.append({
            "title": f"经济日历 · {item.get('name') or '待公布事件'}",
            "detail": detail,
        })
    official = [item for item in news if item.get("official")][:2]
    for item in official:
        items.append({
            "title": f"官方发布 · {item['source']}",
            "detail": f"{item['title']}（{item['published_display']}）",
            "url": item["url"],
        })
    if len(items) < 3:
        items.append({
            "title": "信息核对",
            "detail": "本次官方信息较少；不要用社交媒体传闻替代正式来源和已完成市场数据。",
        })
    return items


def generate_briefing(
    root: Path, *, plan: dict, advice: dict, extra: dict,
    price_file_sha256: str | None = None,
    context_file_sha256: str | None = None,
    advice_signature: str | None = None,
    plan_signature: str | None = None,
) -> dict:
    """Fetch current sources and build one durable, source-attributed briefing."""
    root = Path(root)
    now = datetime.now(SHANGHAI)
    core, market_as_of = read_core_market(root / "data" / "source_adjusted_close.csv")
    macro, macro_warnings = read_macro(root / "data" / "market_context.json")
    # Independent external sources run together so one slow source cannot turn
    # the manual action into a long serial wait.
    with ThreadPoolExecutor(max_workers=4) as executor:
        broad_future = executor.submit(fetch_broad_market, market_as_of)
        news_future = executor.submit(fetch_news, now)
        bls_future = executor.submit(fetch_bls_indicators, now)
        calendar_future = executor.submit(fetch_nasdaq_calendar, now)
        broad, broad_warnings = broad_future.result()
        news, source_statuses, window_hours, news_warnings = news_future.result()
        official_indicators, bls_status, bls_warnings = bls_future.result()
        calendar, calendar_statuses, calendar_warnings = calendar_future.result()
    source_statuses = [*source_statuses, bls_status, *calendar_statuses]
    digest = _market_digest(core, broad, macro, plan)
    charts = _market_charts(core, broad)
    plan_notes = _plan_notes(plan, advice, extra)
    warnings = list(dict.fromkeys([
        *broad_warnings, *macro_warnings, *news_warnings,
        *bls_warnings, *calendar_warnings,
    ]))
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "complete",
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_display": now.strftime("%Y-%m-%d %H:%M"),
        "market_as_of": market_as_of,
        "price_file_sha256": price_file_sha256,
        "context_file_sha256": context_file_sha256,
        "advice_signature": advice_signature,
        "plan_signature": plan_signature,
        "news_window_hours": window_hours,
        "headline": digest["headline"],
        "summary": (
            f"市场日线截至 {market_as_of}；新闻覆盖最近 {window_hours} 小时。"
            "晨报用于早晨了解行情和月内择机核对，不会修改已锁定预算或写入交易。"
        ),
        "market_digest": digest,
        "core_etfs": [{key: value for key, value in item.items() if key != "history"}
                      for item in core],
        "broad_market": [
            {key: value for key, value in item.items()
             if key not in ("history", "chart_only")}
            for item in broad if not item.get("chart_only")
        ],
        "charts": charts,
        "macro": macro,
        "official_indicators": official_indicators,
        "calendar": calendar,
        "plan_notes": plan_notes,
        "today_watch": _today_watch(
            digest, macro, news, calendar, market_as_of, now
        ),
        "news": news,
        "source_statuses": source_statuses,
        "warnings": warnings,
        "boundary": (
            "本晨报使用已完成日线和公开信息，不是盘中实时行情、收益承诺或交易指令。"
            "晨报用于月内择机核对市场环境；不会修改已锁定预算或生成订单。实际下单前须在 Schwab 核对价格、价差、方向和金额。"
        ),
        "stale": False,
    }
    cache = root / "data" / "morning_briefing.json"
    report = root / "reports" / "美股深度晨报.html"
    report_content = build_html_report(result)
    _atomic_text(report, report_content)
    _atomic_json(cache, result)
    return result


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else "—"), quote=True)


def _signed_pct(value: object) -> str:
    number = _finite(value)
    if number is None:
        return "—"
    return f"{number:+.2f}%"


def _market_rows(items: list[dict]) -> str:
    rows = []
    for item in items:
        close = str(item.get("close") or "—") + str(item.get("unit") or "")
        rows.append(
            "<tr>"
            f"<td><strong>{_esc(item.get('label') or item.get('symbol'))}</strong>"
            f"<small>{_esc(item.get('ticker') or item.get('symbol') or '')}</small></td>"
            f"<td class='num'>{_esc(close)}</td>"
            f"<td class='num'>{_esc(_signed_pct(item.get('change_1d_pct')))}</td>"
            f"<td class='num'>{_esc(_signed_pct(item.get('change_5d_pct')))}</td>"
            f"<td class='num'>{_esc(_signed_pct(item.get('change_1m_pct')))}</td>"
            f"<td>{_esc(item.get('date'))}</td>"
            "</tr>"
        )
    return "".join(rows) or "<tr><td colspan='6' class='empty'>本次未取得有效数据。</td></tr>"


def _chart_svg(spec: dict) -> str:
    width, height = 760, 255
    left, right, top, bottom = 52, 18, 18, 33
    parsed: list[dict] = []
    all_values: list[float] = []
    all_dates: list[date] = []
    for item in spec.get("series") or []:
        points = []
        for point in item.get("points") or []:
            try:
                day = date.fromisoformat(str(point.get("date") or ""))
            except ValueError:
                continue
            value = _finite(point.get("value"))
            if value is None:
                continue
            points.append((day, value))
            all_values.append(value)
            all_dates.append(day)
        if len(points) >= 2:
            color = str(item.get("color") or "#5d7f73")
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                color = "#5d7f73"
            parsed.append({"name": str(item.get("name") or "市场"),
                           "color": color, "points": points})
    thresholds = []
    for item in spec.get("thresholds") or []:
        value = _finite(item.get("value"))
        if value is not None:
            thresholds.append((value, str(item.get("label") or _number(value, 1))))
            all_values.append(value)
    if spec.get("zero_line"):
        all_values.append(0.0)
    if not parsed or not all_dates or not all_values:
        return "<div class='empty'>本次没有足够数据绘制走势图。</div>"
    first_day, last_day = min(all_dates), max(all_dates)
    day_span = max(1, (last_day - first_day).days)
    low, high = min(all_values), max(all_values)
    margin = max(1.0, abs(low) * 0.05) if math.isclose(low, high) else (high - low) * 0.08
    low -= margin
    high += margin
    plot_width, plot_height = width - left - right, height - top - bottom

    def x_value(day: date) -> float:
        return left + (day - first_day).days / day_span * plot_width

    def y_value(value: float) -> float:
        return top + (high - value) / (high - low) * plot_height

    unit = str(spec.get("unit") or "")
    grid = []
    for index in range(5):
        value = high - index * (high - low) / 4
        y = y_value(value)
        label = f"{value:+.1f}{unit}" if unit == "%" else f"{value:.1f}{unit}"
        grid.append(
            f"<line x1='{left}' y1='{y:.1f}' x2='{width-right}' y2='{y:.1f}' class='chart-gridline'/>"
            f"<text x='{left-7}' y='{y+4:.1f}' text-anchor='end' class='chart-axis-label'>{_esc(label)}</text>"
        )
    threshold_svg = "".join(
        f"<line x1='{left}' y1='{y_value(value):.1f}' x2='{width-right}' y2='{y_value(value):.1f}' class='chart-threshold'/>"
        f"<text x='{width-right-3}' y='{y_value(value)-4:.1f}' text-anchor='end' class='chart-threshold-label'>{_esc(label)}</text>"
        for value, label in thresholds
    )
    lines = []
    legend = []
    for item in parsed:
        coordinates = " ".join(
            f"{x_value(day):.1f},{y_value(value):.1f}" for day, value in item["points"]
        )
        latest = item["points"][-1][1]
        latest_text = f"{latest:+.2f}{unit}" if unit == "%" else f"{latest:.2f}{unit}"
        lines.append(
            f"<polyline points='{coordinates}' fill='none' stroke='{item['color']}' "
            "stroke-width='2.4' stroke-linejoin='round' stroke-linecap='round' vector-effect='non-scaling-stroke'/>"
        )
        legend.append(
            f"<span><i style='background:{item['color']}'></i>{_esc(item['name'])} "
            f"<b>{_esc(latest_text)}</b></span>"
        )
    return (
        f"<div class='chart-legend'>{''.join(legend)}</div>"
        f"<svg viewBox='0 0 {width} {height}' role='img' aria-label='{_esc(spec.get('title'))}'>"
        f"{''.join(grid)}{threshold_svg}{''.join(lines)}"
        f"<text x='{left}' y='{height-8}' class='chart-axis-label'>{first_day.isoformat()}</text>"
        f"<text x='{width-right}' y='{height-8}' text-anchor='end' class='chart-axis-label'>{last_day.isoformat()}</text>"
        "</svg>"
    )


def build_html_report(data: dict) -> str:
    digest = data.get("market_digest") or {}
    bullets = "".join(
        f"<li><strong>{_esc(item.get('title'))}</strong><span>{_esc(item.get('detail'))}</span></li>"
        for item in digest.get("bullets") or []
    )
    warnings = "".join(f"<li>{_esc(item)}</li>" for item in data.get("warnings") or [])
    watch = "".join(
        "<li>" + (
            f"<a href='{_esc(item['url'])}' target='_blank' rel='noopener noreferrer'>"
            if item.get("url") else ""
        ) + f"<strong>{_esc(item.get('title'))}</strong><span>{_esc(item.get('detail'))}</span>" + (
            "</a>" if item.get("url") else ""
        ) + "</li>"
        for item in data.get("today_watch") or []
    )
    news_cards = "".join(
        "<article class='news-card'>"
        f"<div class='news-meta'><span>{_esc(item.get('category'))}</span>"
        f"<time>{_esc(item.get('published_display'))}</time></div>"
        f"<h3><a href='{_esc(item.get('url'))}' target='_blank' rel='noopener noreferrer'>{_esc(item.get('title'))}</a></h3>"
        f"<p>{_esc(item.get('impact_note'))}</p>"
        f"<small>来源：{_esc(item.get('source'))}{' · 官方来源' if item.get('official') else ''}</small>"
        "</article>"
        for item in data.get("news") or []
    ) or "<div class='empty'>本次未取得可验证的近期新闻，请查看来源状态后重试。</div>"
    source_rows = "".join(
        "<tr>"
        f"<td><a href='{_esc(item.get('url'))}' target='_blank' rel='noopener noreferrer'>{_esc(item.get('name'))}</a></td>"
        f"<td>{'官方' if item.get('official') else '媒体/聚合'}</td>"
        f"<td>{'成功' if item.get('status') == 'ok' else '失败'}</td>"
        f"<td>{_esc(item.get('item_count', 0))}</td>"
        f"<td>{_esc(item.get('fetched_at'))}</td>"
        "</tr>"
        for item in data.get("source_statuses") or []
    )
    va_note = (data.get("plan_notes") or {}).get("va") or {}
    funding = (data.get("plan_notes") or {}).get("funding") or {}
    chart_cards = "".join(
        "<article class='chart-card'>"
        f"<h3>{_esc(item.get('title'))}</h3><p>{_esc(item.get('subtitle'))}</p>"
        f"{_chart_svg(item)}</article>"
        for item in data.get("charts") or []
    ) or "<div class='empty'>本次没有足够数据绘制走势图。</div>"
    macro_cards = "".join(
        "<div class='stat'>"
        f"<span>{_esc(item.get('label'))}</span><strong>{_esc(item.get('close'))}{_esc(item.get('unit') or '')}</strong>"
        f"<small>1 日 { _esc(item.get('change_1d_display') or '—') } · { _esc(item.get('date')) }</small>"
        "</div>"
        for item in data.get("macro") or []
    )
    indicator_cards = "".join(
        "<div class='stat'>"
        f"<span>{_esc(item.get('label'))}</span><strong>{_esc(item.get('value'))}{_esc(item.get('unit') or '')}</strong>"
        f"<small>{_esc(item.get('period'))}{' · 初值' if item.get('preliminary') else ''}</small>"
        "</div>"
        for item in data.get("official_indicators") or []
    ) or "<div class='empty'>本次未取得 BLS 官方指标。</div>"
    calendar = data.get("calendar") or {}
    economic_rows = "".join(
        "<tr>"
        f"<td>{_esc(item.get('time'))}</td><td><strong>{_esc(item.get('name'))}</strong></td>"
        f"<td>{_esc(item.get('consensus') or '—')}</td><td>{_esc(item.get('previous') or '—')}</td>"
        "</tr>"
        for item in calendar.get("economic_events") or []
    ) or "<tr><td colspan='4' class='empty'>未取得该日美国经济事件。</td></tr>"
    earnings_rows = "".join(
        "<tr>"
        f"<td><strong>{_esc(item.get('symbol'))}</strong><small>{_esc(item.get('name'))}</small></td>"
        f"<td>{_esc(item.get('time'))}</td><td>{_esc(item.get('eps_forecast') or '—')}</td>"
        f"<td>{_esc(item.get('market_cap') or '—')}</td>"
        "</tr>"
        for item in calendar.get("earnings") or []
    ) or "<tr><td colspan='4' class='empty'>未取得该日重点财报。</td></tr>"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>美股深度晨报 · {_esc(data.get('generated_display'))}</title>
<style>
:root{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;color:#17332d;background:#f3f6f4}}*{{box-sizing:border-box}}body{{margin:0}}main{{width:min(1120px,calc(100% - 32px));margin:32px auto 70px}}.hero{{padding:34px;border-radius:22px;background:linear-gradient(125deg,#153a33,#245b4c);color:white}}.eyebrow{{font-size:11px;letter-spacing:.17em;color:#a7d4c2;font-weight:800}}h1{{font-size:clamp(28px,5vw,45px);margin:11px 0 12px;letter-spacing:-.04em}}.hero p{{color:#c6ded4;line-height:1.7;max-width:820px}}.meta{{display:flex;gap:16px;flex-wrap:wrap;margin-top:20px;font-size:12px;color:#b9d4ca}}section{{margin-top:20px;background:white;border:1px solid #e1e9e5;border-radius:17px;padding:25px;box-shadow:0 8px 28px #17382d08}}h2{{font-size:19px;margin:0 0 16px}}h3{{font-size:16px;line-height:1.45;margin:9px 0}}.summary-list,.watch-list{{list-style:none;padding:0;margin:0;display:grid;gap:10px}}.summary-list li,.watch-list li{{border:1px solid #e4ebe7;border-radius:11px;padding:13px 15px;display:grid;gap:5px}}li span{{color:#627970;font-size:13px;line-height:1.6}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:18px}}.plan{{border-radius:13px;padding:17px;background:#f4f8f5}}.plan strong{{display:block;margin-bottom:7px}}.plan p{{margin:0;color:#61766e;font-size:13px;line-height:1.65}}.stats{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-bottom:17px}}.stat{{border:1px solid #e4ebe7;border-radius:11px;padding:15px}}.stat span,.stat small{{display:block;color:#71867e;font-size:11px}}.stat strong{{font-size:23px;display:block;margin:7px 0}}.table-wrap{{overflow:auto}}table{{width:100%;border-collapse:collapse;min-width:700px}}th,td{{padding:12px;border-bottom:1px solid #e9efec;text-align:left;font-size:12px}}th{{color:#6e837a;background:#f8faf9}}td small{{display:block;color:#82958e;margin-top:3px}}.num{{text-align:right}}.chart-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:13px}}.chart-card{{border:1px solid #e2eae5;border-radius:13px;padding:17px;min-width:0}}.chart-card h3{{margin:0 0 5px}}.chart-card p{{margin:0 0 10px;color:#71867e;font-size:12px;line-height:1.55}}.chart-card svg{{display:block;width:100%;height:auto}}.chart-gridline{{stroke:#e4ebe7;stroke-width:1}}.chart-axis-label{{fill:#7a8d85;font-size:10px}}.chart-threshold{{stroke:#c18a42;stroke-width:1.2;stroke-dasharray:5 4}}.chart-threshold-label{{fill:#9b713b;font-size:10px}}.chart-legend{{display:flex;gap:12px;flex-wrap:wrap;color:#627970;font-size:11px}}.chart-legend span{{display:flex;align-items:center;gap:5px}}.chart-legend i{{display:inline-block;width:9px;height:9px;border-radius:50%}}.news-grid{{display:grid;grid-template-columns:1fr 1fr;gap:13px}}.news-card{{border:1px solid #e2eae5;border-radius:13px;padding:17px}}.news-card p{{color:#657970;font-size:13px;line-height:1.65}}.news-card small{{color:#82938c}}.news-meta{{display:flex;justify-content:space-between;gap:12px;color:#55756a;font-size:11px}}a{{color:inherit;text-decoration:none}}a:hover{{text-decoration:underline}}.notice{{background:#fff8ea;border-color:#eadcbf;color:#775d2b}}.notice ul{{margin:7px 0;padding-left:20px;line-height:1.7;font-size:13px}}.boundary{{font-size:12px;line-height:1.7;color:#647970}}.empty{{color:#7e9189;padding:25px;text-align:center}}footer{{text-align:center;color:#778b83;font-size:11px;margin-top:22px;line-height:1.7}}@media(max-width:760px){{main{{width:min(100% - 20px,1120px);margin-top:12px}}.hero,section{{padding:20px}}.grid,.news-grid,.stats,.chart-grid{{grid-template-columns:1fr}}}}
</style></head><body><main>
<header class="hero"><div class="eyebrow">U.S. MARKET MORNING BRIEFING</div><h1>{_esc(data.get('headline'))}</h1><p>{_esc(data.get('summary'))}</p><div class="meta"><span>生成：{_esc(data.get('generated_display'))}（上海时间）</span><span>市场日线：{_esc(data.get('market_as_of'))}</span><span>新闻窗口：最近 {_esc(data.get('news_window_hours'))} 小时</span></div></header>
<section><h2>一分钟结论</h2><ul class="summary-list">{bullets}</ul></section>
<div class="grid"><section><h2>与你的计划有关</h2><div class="plan"><strong>{_esc(va_note.get('title'))}</strong><p>{_esc(va_note.get('detail'))}</p></div><div class="plan" style="margin-top:10px"><strong>{_esc(funding.get('title'))}</strong><p>{_esc(funding.get('detail'))}</p></div></section><section><h2>今天重点关注</h2><ul class="watch-list">{watch}</ul></section></div>
<section><h2>市场走势图</h2><div class="chart-grid">{chart_cards}</div></section>
<section><h2>核心市场快照</h2><div class="stats">{macro_cards}</div><div class="table-wrap"><table><thead><tr><th>项目</th><th class="num">收盘</th><th class="num">1 日</th><th class="num">5 日</th><th class="num">1 个月</th><th>数据日</th></tr></thead><tbody>{_market_rows([*(data.get('core_etfs') or []), *(data.get('broad_market') or [])])}</tbody></table></div></section>
<section><h2>BLS 官方宏观指标</h2><div class="stats">{indicator_cards}</div></section>
<div class="grid"><section><h2>{_esc(calendar.get('date'))} 美国经济日历</h2><div class="table-wrap"><table><thead><tr><th>时间</th><th>事件</th><th>预期</th><th>前值</th></tr></thead><tbody>{economic_rows}</tbody></table></div><p class="boundary">{_esc(calendar.get('note'))}</p></section><section><h2>{_esc(calendar.get('date'))} 重点财报</h2><div class="table-wrap"><table><thead><tr><th>公司</th><th>时间</th><th>EPS 预期</th><th>市值</th></tr></thead><tbody>{earnings_rows}</tbody></table></div></section></div>
<section><h2>重要新闻与可能影响</h2><div class="news-grid">{news_cards}</div></section>
{f'<section class="notice"><h2>数据提示</h2><ul>{warnings}</ul></section>' if warnings else ''}
<section><h2>来源与获取状态</h2><div class="table-wrap"><table><thead><tr><th>来源</th><th>类型</th><th>状态</th><th>有效条目</th><th>获取时间</th></tr></thead><tbody>{source_rows}</tbody></table></div><p class="boundary">{_esc(data.get('boundary'))}</p></section>
<footer>VOO / QQQM 本地投资工具 · 晨报会覆盖上一份本地晨报；联网生成会同步持仓估算用价格，但不会新增或修改交易流水及 VA 目标路径。</footer>
</main></body></html>"""


__all__ = [
    "SCHEMA_VERSION", "build_html_report", "empty_briefing", "fetch_news",
    "generate_briefing", "load_briefing", "read_core_market",
]
