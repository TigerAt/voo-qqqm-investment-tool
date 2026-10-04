"""Network helpers shared by Yahoo Finance readers.

The local Mac can retain proxy environment variables after a proxy app has
stopped.  yfinance otherwise inherits that dead proxy and reports every ticker
as unavailable.  A dedicated curl_cffi session keeps these public market reads
direct and does not change process-wide environment variables.
"""

from __future__ import annotations


def direct_yfinance_session():
    try:
        from curl_cffi import requests as curl_requests
    except ImportError as exc:
        raise ValueError("yfinance 的网络组件 curl_cffi 不可用。") from exc
    session = curl_requests.Session(impersonate="chrome")
    session.trust_env = False
    session.proxies = {}
    return session


__all__ = ["direct_yfinance_session"]
