# Dehradun Dish Intelligence Agent

Python Playwright-based AI data agent that extracts dish-level intelligence from food delivery platforms (Zomato/Swiggy) for Dehradun areas.

## What it does

- Crawls area-wise listings (default: Jakhan, Race Course, Rajpur Road) and collects restaurant targets without filtering by low rating.
- Opens each restaurant order/menu page and extracts dish-level fields:
  - `dish_name`
  - `price`
  - `aggregate_dish_rating` (stored as `dish_rating`)
  - `total_votes_per_dish` (stored as `dish_votes`)
  - `is_bestseller`
- Applies hidden-gem heuristic (`high_potential_item = True`):
  - restaurant overall rating `< 4.0`
  - dish rating `> 4.5`
  - dish votes `> 10`
- Uses anti-blocking tactics:
  - random user-agent rotation
  - randomized delays/jitter
  - human-like scrolling
  - optional residential proxy via env vars
- Produces a master database in both CSV and JSON.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

## Optional proxy configuration

Create `.env` with:

```env
RES_PROXY_SERVER=http://host:port
RES_PROXY_USERNAME=your_username
RES_PROXY_PASSWORD=your_password
```

## Run

```bash
python agent.py --platform zomato
```

Custom areas / limits:

```bash
python agent.py --platform swiggy --areas "Jakhan" "Race Course" "Rajpur Road" --max-restaurants-per-area 100
```

Output files are generated under `output/`:

- `dehradun_master_database_<timestamp>.json`
- `dehradun_master_database_<timestamp>.csv`

CSV columns:

- `restaurant_name`
- `overall_rating`
- `dish_name`
- `dish_rating`
- `dish_votes`
- `price`
- `location_tag`
- `is_bestseller`
- `high_potential_item`
