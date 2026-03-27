# Dehradun Unified Food Intelligence Agent (v2.0)

Playwright-based data agent for extracting dish-level intelligence from **Zomato + Swiggy** in Dehradun.

## Key capabilities

- Area-wise restaurant discovery for Dehradun (default: Jakhan, Race Course, Rajpur Road).
- No filtering by restaurant rating during discovery (full-spectrum coverage).
- API interception:
  - **Zomato**: intercepts internal `getPage` responses.
  - **Swiggy**: intercepts `/dapi/menu/v4/full` responses.
- Required fields extracted:
  - `platform`, `restaurant_name`, `overall_rating`, `dish_name`, `price`, `dish_rating`, `dish_votes`, `is_bestseller`, `location_tag`
- Hidden gem heuristic:
  - `overall_rating < 4.0` AND `dish_rating > 4.5` AND `dish_votes > 10`
- Market score:
  - `market_score = dish_rating * log10(dish_votes + 1)`

## Security / resilience upgrades

- Proper stealth integration for `playwright-stealth` v2 (`from playwright_stealth import stealth` + `await stealth(page)`).
- macOS/Chrome-like browser fingerprinting (`channel="chrome"`, realistic UA/viewport, language/timezone).
- Header mimicry (`Accept`, `Accept-Language`, `Referer`, fetch hints).
- Randomized delays + human-like scrolling.
- Retry handler for transient blocks (including `ERR_HTTP2_PROTOCOL_ERROR`).
- Residential proxy rotation via `PROXY_URL` (comma-separated pool supported).

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

## Environment config

Create `.env`:

```env
# Single proxy
PROXY_URL=http://username:password@host:port

# OR rotating pool (comma-separated)
# PROXY_URL=http://u:p@ip1:port,http://u:p@ip2:port,http://u:p@ip3:port
```

## Run

```bash
python agent.py --platform zomato
python agent.py --platform swiggy --areas "Jakhan" "Rajpur Road"
```

## Output

- `output/master_market_report.csv` (consolidated master report)
- `output/dehradun_master_database_<timestamp>.json`

## Recommendation

Cloud notebooks and data-center IPs are often blocked by Zomato/Swiggy anti-bot layers. Prefer local execution (Mac/Windows/Linux with residential proxy) for production reliability.


## Troubleshooting

- If you see `ERR_HTTP2_PROTOCOL_ERROR` on Zomato, it is usually IP reputation/fingerprint blocking.
- Add a residential proxy in `.env` using `PROXY_URL=...` and retry.
- The script now skips blocked areas gracefully and continues processing others instead of crashing.
