# MarketDesk — Live NSE + BSE Stock Tracker

A lightweight trading-terminal style web app:

- **Search with autocomplete** over every listed NSE + BSE stock (NSE `EQUITY_L.csv` + BSE scrip API, deduped by ISIN, NSE preferred, cached to disk and refreshed daily).
- **Watchlist** (localStorage) with live price, day %, distance from 52-week high/low, and 1M/3M % change — refreshed every 15s via one batched API call.
- **Live chart** via the free TradingView Advanced Chart embed (`NSE:SYMBOL` / `BSE:SYMBOL`, dark theme).
- **News sidebar** from Google News RSS, parsed server-side (per-stock, or general Indian market news), refreshed every 60s.

## Structure

```
backend/    Flask API (deploy to Railway)
frontend/   Static single-page app (deploy to Netlify)
```

## Run locally

```bash
# Backend (http://127.0.0.1:5000)
cd backend
python -m venv .venv
.venv\Scripts\activate        # Windows  (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
python app.py

# Frontend — serve the static file (http://127.0.0.1:8080)
cd frontend
python -m http.server 8080
```

`frontend/index.html` has `API_BASE` set to `http://127.0.0.1:5000` by default.

## Deploy

### Backend → Railway

1. Push this repo to GitHub and create a new Railway project from it.
2. Set the service **Root Directory** to `backend/`. Railway detects the
   `Procfile` and runs `gunicorn app:app --bind 0.0.0.0:$PORT` (the `PORT`
   env var is injected by Railway automatically — nothing to configure).
3. Note your public URL, e.g. `https://your-app.up.railway.app`.

### Frontend → Netlify

1. Edit the one constant at the top of the `<script>` in `frontend/index.html`:
   ```js
   const API_BASE = "https://your-app.up.railway.app";
   ```
2. Deploy the `frontend/` folder as a Netlify site (drag-and-drop or
   `netlify deploy --dir frontend --prod`). No build step needed.
3. Optional hardening: in `backend/app.py`, replace `CORS(app)` with
   `CORS(app, origins=["https://your-site.netlify.app"])` (a `TODO` comment
   marks the spot).

## API

| Endpoint | Description |
|---|---|
| `/api/search?q=reli` | Autocomplete — symbol or company name, exchange badge |
| `/api/quote/RELIANCE` | LTP, day %, 52W high/low + distance, 1M/3M % |
| `/api/quotes?symbols=RELIANCE,TCS` | Batched quotes (10s server-side cache) |
| `/api/news` / `/api/news/RELIANCE` | Google News RSS headlines |

Data sources: NSE archives (symbol master), BSE API (scrip list, degrades to
NSE-only if unreachable), Yahoo Finance via `yfinance` (quotes, `.NS`/`.BO`),
Google News RSS (headlines). Quotes can be delayed per Yahoo's feed.
