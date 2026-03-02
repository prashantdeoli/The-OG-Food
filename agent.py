#!/usr/bin/env python3
"""AI data agent for dish-level intelligence extraction in Dehradun."""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import random
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fake_useragent import UserAgent
from playwright.async_api import BrowserContext, Page, async_playwright
from playwright_stealth import stealth


DEHRADUN_AREAS = ["Jakhan", "Race Course", "Rajpur Road"]
DEFAULT_OUTPUT_DIR = Path("output")
USER_AGENT_FALLBACKS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]


@dataclass
class Restaurant:
    name: str
    overall_rating: float | None
    location_tag: str
    source_url: str
    order_online_url: str | None = None


@dataclass
class DishRecord:
    restaurant_name: str
    overall_rating: float | None
    dish_name: str
    dish_rating: float | None
    dish_votes: int | None
    price: str | None
    location_tag: str
    is_bestseller: bool
    high_potential_item: bool


class DehradunDishIntelAgent:
    def __init__(
        self,
        platform: str,
        headless: bool,
        min_delay: float,
        max_delay: float,
        max_restaurants_per_area: int,
        output_dir: Path,
    ) -> None:
        self.platform = platform.lower().strip()
        self.headless = headless
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.max_restaurants_per_area = max_restaurants_per_area
        self.output_dir = output_dir
        self.ua_provider = UserAgent(browsers=["chrome", "edge", "firefox", "safari"])

    @staticmethod
    def _proxy_config() -> dict[str, str] | None:
        proxy_server = os.getenv("RES_PROXY_SERVER")
        if not proxy_server:
            return None
        proxy: dict[str, str] = {"server": proxy_server}
        if os.getenv("RES_PROXY_USERNAME"):
            proxy["username"] = os.environ["RES_PROXY_USERNAME"]
        if os.getenv("RES_PROXY_PASSWORD"):
            proxy["password"] = os.environ["RES_PROXY_PASSWORD"]
        return proxy

    def _random_user_agent(self) -> str:
        try:
            return self.ua_provider.random
        except Exception:
            return random.choice(USER_AGENT_FALLBACKS)

    async def _jitter(self) -> None:
        await asyncio.sleep(random.uniform(self.min_delay, self.max_delay))

    async def _human_like_scroll(self, page: Page, cycles: int = 10) -> None:
        for _ in range(cycles):
            distance = random.randint(280, 900)
            await page.mouse.wheel(0, distance)
            await self._jitter()

    async def _build_context(self, playwright) -> BrowserContext:
        browser = await playwright.chromium.launch(headless=self.headless)
        context = await browser.new_context(
            user_agent=self._random_user_agent(),
            viewport={"width": random.randint(1180, 1490), "height": random.randint(700, 980)},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            proxy=self._proxy_config(),
        )
        return context

    def _area_search_url(self, area: str) -> str:
        if self.platform == "zomato":
            normalized = area.lower().replace(" ", "-")
            return f"https://www.zomato.com/dehradun/{normalized}-restaurants"
        if self.platform == "swiggy":
            query = area.replace(" ", "%20")
            return f"https://www.swiggy.com/city/dehradun?search={query}"
        raise ValueError("Unsupported platform. Use 'zomato' or 'swiggy'.")

    async def discover_restaurants(self, page: Page, area: str) -> list[Restaurant]:
        url = self._area_search_url(area)
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)
        await stealth(page)
        await self._jitter()
        await self._human_like_scroll(page, cycles=12)

        if self.platform == "zomato":
            selector = "a[href*='/dehradun/'][href*'/order']"
            cards = page.locator("a[href*='/dehradun/']")
        else:
            cards = page.locator("a[href*='/restaurants/'], a[href*='/menu']")

        discovered: list[Restaurant] = []
        seen = set()
        count = await cards.count()
        for i in range(min(count, self.max_restaurants_per_area * 4)):
            href = await cards.nth(i).get_attribute("href")
            if not href:
                continue
            full_url = href if href.startswith("http") else f"https://www.{self.platform}.com{href}"
            if full_url in seen:
                continue
            seen.add(full_url)

            text = (await cards.nth(i).inner_text()).strip() or f"Restaurant-{len(discovered)+1}"
            rating = self._parse_rating(text)
            discovered.append(
                Restaurant(
                    name=text.split("\n")[0][:120],
                    overall_rating=rating,
                    location_tag=area,
                    source_url=full_url,
                    order_online_url=full_url,
                )
            )
            if len(discovered) >= self.max_restaurants_per_area:
                break

        return discovered

    @staticmethod
    def _parse_rating(text: str) -> float | None:
        m = re.search(r"\b([0-4](?:\.\d)?)\b", text)
        if not m:
            return None
        try:
            value = float(m.group(1))
        except ValueError:
            return None
        return value if 0.0 <= value <= 5.0 else None

    async def extract_dishes(self, page: Page, restaurant: Restaurant) -> list[DishRecord]:
        if not restaurant.order_online_url:
            return []

        url = restaurant.order_online_url
        if self.platform == "zomato" and "/order" not in url:
            url = url.rstrip("/") + "/order"

        await page.goto(url, wait_until="domcontentloaded", timeout=90000)
        await stealth(page)
        await self._jitter()
        await self._human_like_scroll(page, cycles=8)

        dishes = await self._extract_from_dom(page, restaurant)
        if not dishes:
            dishes = await self._extract_from_structured_data(page, restaurant)
        return dishes

    async def _extract_from_dom(self, page: Page, restaurant: Restaurant) -> list[DishRecord]:
        rows = page.locator("div:has-text('₹')")
        dish_records: list[DishRecord] = []
        row_count = await rows.count()

        for i in range(min(row_count, 500)):
            row = rows.nth(i)
            text = (await row.inner_text()).strip()
            if not text or len(text) < 5:
                continue

            price_match = re.search(r"₹\s?\d+(?:\.\d{1,2})?", text)
            dish_name = text.split("\n")[0].strip()
            rating = self._parse_inline_rating(text)
            votes = self._parse_votes(text)
            is_bestseller = bool(re.search(r"best\s*seller", text, flags=re.IGNORECASE))
            if not price_match or len(dish_name) < 2:
                continue

            dish_records.append(
                DishRecord(
                    restaurant_name=restaurant.name,
                    overall_rating=restaurant.overall_rating,
                    dish_name=dish_name,
                    dish_rating=rating,
                    dish_votes=votes,
                    price=price_match.group(0),
                    location_tag=restaurant.location_tag,
                    is_bestseller=is_bestseller,
                    high_potential_item=self._is_high_potential(restaurant.overall_rating, rating, votes),
                )
            )

        return self._dedupe_dishes(dish_records)

    async def _extract_from_structured_data(self, page: Page, restaurant: Restaurant) -> list[DishRecord]:
        script_nodes = page.locator("script[type='application/ld+json']")
        records: list[DishRecord] = []
        for idx in range(await script_nodes.count()):
            payload = await script_nodes.nth(idx).inner_text()
            try:
                data = json.loads(payload)
            except Exception:
                continue

            items = data if isinstance(data, list) else [data]
            for item in items:
                menu_items = item.get("hasMenuItem") if isinstance(item, dict) else None
                if not isinstance(menu_items, list):
                    continue
                for dish in menu_items:
                    if not isinstance(dish, dict):
                        continue
                    rating_data = dish.get("aggregateRating") or {}
                    rating = self._as_float(rating_data.get("ratingValue"))
                    votes = self._as_int(rating_data.get("ratingCount"))
                    price = dish.get("offers", {}).get("price")
                    records.append(
                        DishRecord(
                            restaurant_name=restaurant.name,
                            overall_rating=restaurant.overall_rating,
                            dish_name=(dish.get("name") or "Unknown Dish").strip(),
                            dish_rating=rating,
                            dish_votes=votes,
                            price=f"₹{price}" if price else None,
                            location_tag=restaurant.location_tag,
                            is_bestseller=False,
                            high_potential_item=self._is_high_potential(restaurant.overall_rating, rating, votes),
                        )
                    )

        return self._dedupe_dishes(records)

    @staticmethod
    def _as_float(value: Any) -> float | None:
        try:
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _as_int(value: Any) -> int | None:
        try:
            return int(float(value))
        except Exception:
            return None

    @staticmethod
    def _parse_inline_rating(text: str) -> float | None:
        pattern = re.search(r"(?:rating|\★)?\s*([0-5](?:\.\d)?)", text, flags=re.IGNORECASE)
        if not pattern:
            return None
        try:
            val = float(pattern.group(1))
            return val if 0 <= val <= 5 else None
        except ValueError:
            return None

    @staticmethod
    def _parse_votes(text: str) -> int | None:
        m = re.search(r"(\d+)\s*(?:votes?|ratings?)", text, flags=re.IGNORECASE)
        if m:
            return int(m.group(1))
        m2 = re.search(r"\((\d+)\)", text)
        return int(m2.group(1)) if m2 else None

    @staticmethod
    def _is_high_potential(overall: float | None, dish_rating: float | None, votes: int | None) -> bool:
        if overall is None or dish_rating is None or votes is None:
            return False
        return overall < 4.0 and dish_rating > 4.5 and votes > 10

    @staticmethod
    def _dedupe_dishes(items: list[DishRecord]) -> list[DishRecord]:
        seen = set()
        deduped: list[DishRecord] = []
        for item in items:
            key = (item.restaurant_name.lower(), item.dish_name.lower(), item.price)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    async def run(self, areas: list[str]) -> list[DishRecord]:
        dish_rows: list[DishRecord] = []
        async with async_playwright() as p:
            context = await self._build_context(p)
            page = await context.new_page()

            for area in areas:
                restaurants = await self.discover_restaurants(page, area)
                for restaurant in restaurants:
                    try:
                        dish_rows.extend(await self.extract_dishes(page, restaurant))
                    except Exception as exc:
                        print(f"[WARN] Skipping {restaurant.name}: {exc}")
                        continue

            await context.close()

        return dish_rows

    def save(self, rows: list[DishRecord]) -> tuple[Path, Path]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        json_path = self.output_dir / f"dehradun_master_database_{ts}.json"
        csv_path = self.output_dir / f"dehradun_master_database_{ts}.csv"

        payload = [asdict(x) for x in rows]
        with json_path.open("w", encoding="utf-8") as jf:
            json.dump(payload, jf, indent=2, ensure_ascii=False)

        with csv_path.open("w", newline="", encoding="utf-8") as cf:
            writer = csv.DictWriter(
                cf,
                fieldnames=[
                    "restaurant_name",
                    "overall_rating",
                    "dish_name",
                    "dish_rating",
                    "dish_votes",
                    "price",
                    "location_tag",
                    "is_bestseller",
                    "high_potential_item",
                ],
            )
            writer.writeheader()
            writer.writerows(payload)

        return json_path, csv_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract dish-level intelligence for Dehradun.")
    parser.add_argument("--platform", default="zomato", choices=["zomato", "swiggy"])
    parser.add_argument("--areas", nargs="*", default=DEHRADUN_AREAS)
    parser.add_argument("--headed", action="store_true", help="Run browser in headed mode.")
    parser.add_argument("--min-delay", type=float, default=0.7)
    parser.add_argument("--max-delay", type=float, default=2.2)
    parser.add_argument("--max-restaurants-per-area", type=int, default=60)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    return parser.parse_args()


async def main() -> None:
    load_dotenv()
    args = parse_args()

    agent = DehradunDishIntelAgent(
        platform=args.platform,
        headless=not args.headed,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        max_restaurants_per_area=args.max_restaurants_per_area,
        output_dir=Path(args.output_dir),
    )

    rows = await agent.run(args.areas)
    json_path, csv_path = agent.save(rows)
    print(f"Saved {len(rows)} rows")
    print(f"JSON: {json_path}")
    print(f"CSV:  {csv_path}")


if __name__ == "__main__":
    asyncio.run(main())
