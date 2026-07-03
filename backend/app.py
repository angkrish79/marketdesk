"""Live NSE + BSE stock tracker — Flask backend.

Endpoints:
  GET /api/search?q=<text>         autocomplete over the full NSE+BSE symbol master
  GET /api/quote/<symbol>          full quote (LTP, day %, 52W stats, 1M/3M %)
  GET /api/quotes?symbols=A,B,C    batched quotes for the watchlist
  GET /api/news                    general Indian market news (Google News RSS)
  GET /api/news/<symbol>           news for one stock
"""

import csv
import io
import json
import logging
import os
import threading
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import pandas as pd
import requests
import yfinance as yf
from flask import Flask, jsonify, request
from flask_cors import CORS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tracker")

app = Flask(__name__)
# CORS: open to all origins for now.
# TODO: lock down once the Netlify domain is known, e.g.:
#   CORS(app, origins=["https://your-app.netlify.app"])
CORS(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

NSE_CSV_PATH = os.path.join(CACHE_DIR, "EQUITY_L.csv")
BSE_JSON_PATH = os.path.join(CACHE_DIR, "bse_scrips.json")
MASTER_MAX_AGE = 24 * 3600  # refresh the symbol master daily

NSE_CSV_URLS = [
    "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
    "https://archives.nseindia.com/content/equities/EQUITY_L.csv",
]
BSE_SCRIP_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
    "?Group=&Scripcode=&industry=&segment=Equity&status=Active"
)

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
NSE_HEADERS = {"User-Agent": BROWSER_UA, "Accept": "*/*"}
BSE_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Referer": "https://www.bseindia.com/",
    "Accept": "application/json, text/plain, */*",
}

# ---------------------------------------------------------------------------
# Symbol master (search index)
# ---------------------------------------------------------------------------

_symbols = []      # [{"symbol", "name", "exchange"}] sorted for search
_symbol_map = {}   # symbol -> entry, for quote/news lookups
_master_lock = threading.Lock()
_master_ready = threading.Event()


def _cache_is_fresh(path):
    return os.path.exists(path) and (time.time() - os.path.getmtime(path)) < MASTER_MAX_AGE


def _fetch_to_cache(urls, headers, path, label):
    """Download the first URL that works into `path`. Returns True on success.

    On total failure, keeps any stale cache file that already exists.
    """
    for url in urls:
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            if len(resp.content) < 1000:
                raise ValueError(f"suspiciously small response ({len(resp.content)} bytes)")
            with open(path, "wb") as f:
                f.write(resp.content)
            log.info("%s: downloaded %d bytes from %s", label, len(resp.content), url)
            return True
        except Exception as exc:
            log.warning("%s: fetch failed from %s: %s", label, url, exc)
    return False


def _load_nse():
    """Parse EQUITY_L.csv -> list of (symbol, name, isin)."""
    if not _cache_is_fresh(NSE_CSV_PATH):
        _fetch_to_cache(NSE_CSV_URLS, NSE_HEADERS, NSE_CSV_PATH, "NSE EQUITY_L.csv")
    if not os.path.exists(NSE_CSV_PATH):
        return []
    rows = []
    with open(NSE_CSV_PATH, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        # header names sometimes carry stray spaces
        for raw in reader:
            row = {k.strip(): (v or "").strip() for k, v in raw.items() if k}
            symbol = row.get("SYMBOL")
            name = row.get("NAME OF COMPANY")
            isin = row.get("ISIN NUMBER", "")
            if symbol and name:
                rows.append((symbol, name, isin))
    return rows


def _load_bse():
    """Fetch BSE active equity scrips -> list of (symbol, name, isin).

    Degrades gracefully: on failure returns whatever stale cache exists, or [].
    """
    if not _cache_is_fresh(BSE_JSON_PATH):
        _fetch_to_cache([BSE_SCRIP_URL], BSE_HEADERS, BSE_JSON_PATH, "BSE scrip list")
    if not os.path.exists(BSE_JSON_PATH):
        log.warning("BSE scrip list unavailable — search will be NSE-only")
        return []
    try:
        with open(BSE_JSON_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        log.warning("BSE scrip list unreadable (%s) — search will be NSE-only", exc)
        return []
    rows = []
    for item in data:
        symbol = (item.get("scrip_id") or "").strip()
        name = (item.get("Issuer_Name") or item.get("Scrip_Name") or "").strip()
        isin = (item.get("ISIN_NUMBER") or "").strip()
        if symbol and name:
            rows.append((symbol, name, isin))
    return rows


def build_master():
    """Build the merged NSE+BSE search index. NSE wins on ISIN duplicates."""
    nse = _load_nse()
    bse = _load_bse()

    entries = []
    seen_isins = set()
    seen_keys = set()  # (symbol, exchange) safety net

    for symbol, name, isin in nse:
        entries.append({"symbol": symbol, "name": name, "exchange": "NSE"})
        if isin:
            seen_isins.add(isin)
        seen_keys.add(symbol)

    skipped = 0
    for symbol, name, isin in bse:
        if (isin and isin in seen_isins) or symbol in seen_keys:
            skipped += 1
            continue
        entries.append({"symbol": symbol, "name": name, "exchange": "BSE"})
        seen_keys.add(symbol)

    with _master_lock:
        global _symbols, _symbol_map
        _symbols = entries
        _symbol_map = {e["symbol"].upper(): e for e in entries}
    _master_ready.set()
    log.info(
        "Symbol master built: %d NSE + %d BSE-only (%d duplicates deduped, NSE preferred)",
        len(nse), len(entries) - len(nse), skipped,
    )


def _master_refresher():
    while True:
        try:
            build_master()
        except Exception:
            log.exception("Symbol master build failed")
            _master_ready.set()  # don't wedge requests forever; serve what we have
        time.sleep(MASTER_MAX_AGE)


threading.Thread(target=_master_refresher, daemon=True).start()


# ---------------------------------------------------------------------------
# Quotes (yfinance)
# ---------------------------------------------------------------------------

QUOTE_TTL = 10  # seconds — protects yfinance from the 15s watchlist refresh
_quote_cache = {}  # symbol -> (timestamp, payload)
_quote_lock = threading.Lock()


def _yahoo_ticker(entry):
    suffix = ".NS" if entry["exchange"] == "NSE" else ".BO"
    return entry["symbol"] + suffix


def _pct(cur, base):
    if base is None or cur is None or base == 0:
        return None
    return round((cur - base) / base * 100, 2)


def _compute_metrics(entry, df):
    """Turn a daily OHLC DataFrame into the quote payload."""
    closes = df["Close"].dropna()
    if closes.empty:
        return None
    ltp = float(closes.iloc[-1])
    prev_close = float(closes.iloc[-2]) if len(closes) >= 2 else None

    tail = df.tail(252)
    high_52w = float(tail["High"].max())
    low_52w = float(tail["Low"].min())

    close_1m = float(closes.iloc[-22]) if len(closes) >= 22 else None
    close_3m = float(closes.iloc[-64]) if len(closes) >= 64 else None

    return {
        "symbol": entry["symbol"],
        "name": entry["name"],
        "exchange": entry["exchange"],
        "tv_symbol": f"{entry['exchange']}:{entry['symbol']}",
        "ltp": round(ltp, 2),
        "day_change_pct": _pct(ltp, prev_close),
        "high_52w": round(high_52w, 2),
        "low_52w": round(low_52w, 2),
        "pct_from_high": _pct(ltp, high_52w),  # negative = below 52W high
        "pct_from_low": _pct(ltp, low_52w),    # positive = above 52W low
        "change_1m_pct": _pct(ltp, close_1m),
        "change_3m_pct": _pct(ltp, close_3m),
        "currency": "INR",
    }


def get_quotes(symbols):
    """Batched quotes with a short server-side cache."""
    now = time.time()
    results = {}
    to_fetch = []

    with _quote_lock:
        for sym in symbols:
            cached = _quote_cache.get(sym)
            if cached and now - cached[0] < QUOTE_TTL:
                results[sym] = cached[1]
            else:
                to_fetch.append(sym)

    if to_fetch:
        entries = {}
        for sym in to_fetch:
            entry = _symbol_map.get(sym.upper())
            if entry:
                entries[_yahoo_ticker(entry)] = (sym, entry)
            else:
                results[sym] = {"symbol": sym, "error": "Unknown symbol"}

        if entries:
            tickers = list(entries.keys())
            try:
                data = yf.download(
                    tickers=tickers,
                    period="1y",
                    interval="1d",
                    group_by="ticker",
                    auto_adjust=False,
                    threads=True,
                    progress=False,
                )
            except Exception as exc:
                log.warning("yfinance download failed for %s: %s", tickers, exc)
                data = None

            for yahoo_sym, (orig_sym, entry) in entries.items():
                payload = None
                try:
                    if data is not None and not data.empty:
                        # group_by="ticker" yields MultiIndex columns (ticker, field),
                        # even for a single ticker on recent yfinance versions
                        if isinstance(data.columns, pd.MultiIndex):
                            df = data[yahoo_sym] if yahoo_sym in data.columns.get_level_values(0) else None
                        else:
                            df = data
                        if df is not None:
                            df = df.dropna(how="all")
                            if not df.empty:
                                payload = _compute_metrics(entry, df)
                except Exception as exc:
                    log.warning("metric computation failed for %s: %s", yahoo_sym, exc)
                if payload is None:
                    payload = {
                        "symbol": entry["symbol"],
                        "name": entry["name"],
                        "exchange": entry["exchange"],
                        "error": "No price data available",
                    }
                results[orig_sym] = payload
                with _quote_lock:
                    _quote_cache[orig_sym] = (time.time(), payload)

    return [results[s] for s in symbols if s in results]


# ---------------------------------------------------------------------------
# News (Google News RSS, parsed server-side to dodge CORS)
# ---------------------------------------------------------------------------

NEWS_TTL = 60
_news_cache = {}  # query -> (timestamp, items)
_news_lock = threading.Lock()


def fetch_news(query, limit=12):
    with _news_lock:
        cached = _news_cache.get(query)
        if cached and time.time() - cached[0] < NEWS_TTL:
            return cached[1]

    url = "https://news.google.com/rss/search"
    params = {"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}
    items = []
    try:
        resp = requests.get(url, params=params, headers={"User-Agent": BROWSER_UA}, timeout=15)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        for item in root.iter("item"):
            title = item.findtext("title") or ""
            link = item.findtext("link") or ""
            source = item.findtext("source") or ""
            pub = item.findtext("pubDate") or ""
            published = None
            try:
                published = parsedate_to_datetime(pub).isoformat()
            except Exception:
                pass
            if title and link:
                items.append({"title": title, "link": link, "source": source, "published": published})
            if len(items) >= limit:
                break
    except Exception as exc:
        log.warning("news fetch failed for %r: %s", query, exc)
        return None  # signals fetch failure (distinct from "no results")

    with _news_lock:
        _news_cache[query] = (time.time(), items)
    return items


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def health():
    return jsonify({
        "status": "ok",
        "service": "nse-bse-stock-tracker",
        "symbols_loaded": len(_symbols),
    })


@app.get("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip().lower()
    if not q:
        return jsonify({"results": []})
    if not _master_ready.is_set():
        return jsonify({"results": [], "loading": True})

    scored = []
    with _master_lock:
        universe = _symbols
    for e in universe:
        sym = e["symbol"].lower()
        name = e["name"].lower()
        if sym.startswith(q):
            score = 0
        elif name.startswith(q):
            score = 1
        elif q in sym:
            score = 2
        elif q in name:
            score = 3
        else:
            continue
        scored.append((score, len(sym), e))
    scored.sort(key=lambda t: (t[0], t[1], t[2]["symbol"]))
    return jsonify({"results": [e for _, _, e in scored[:15]]})


@app.get("/api/quote/<symbol>")
def api_quote(symbol):
    quotes = get_quotes([symbol.upper()])
    if not quotes:
        return jsonify({"error": "Unknown symbol"}), 404
    q = quotes[0]
    status = 404 if q.get("error") == "Unknown symbol" else 200
    return jsonify(q), status


@app.get("/api/quotes")
def api_quotes():
    raw = request.args.get("symbols", "")
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    if not symbols:
        return jsonify({"quotes": []})
    if len(symbols) > 50:
        return jsonify({"error": "Too many symbols (max 50)"}), 400
    return jsonify({"quotes": get_quotes(symbols)})


@app.get("/api/news")
@app.get("/api/news/<symbol>")
def api_news(symbol=None):
    if symbol:
        entry = _symbol_map.get(symbol.upper())
        label = entry["name"] if entry else symbol
        query = f'"{label}" stock'
    else:
        label = "Indian stock market"
        query = "Indian stock market NSE BSE"
    items = fetch_news(query)
    if items is None:
        return jsonify({"query": label, "items": [], "error": "News feed unavailable right now"}), 502
    return jsonify({"query": label, "items": items})


if __name__ == "__main__":
    # Local development only — Railway runs this via gunicorn (see Procfile).
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False)
