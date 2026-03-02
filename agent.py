#!/usr/bin/env python3
"""AI data agent for dish-level intelligence extraction in Dehradun."""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import random
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from fake_useragent import UserAgent
from playwright.async_api import BrowserContext, Page, Response, async_playwright
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
    order_online_url: str


@dataclass
class DishRecord:
    platform: str
    restaurant_name: str
    overall_rating: float | None
    dish_name: str
    dish_rating: float | None
    dish_votes: int | None
    price: str | None
    location_tag: str
    is_bestseller: bool
    high_potential_item: bool
    market_score: float | None


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
        self.platform = platform.strip().lower()
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
            await page.mouse.wheel(0, random.randint(280, 900))
            await self._jitter()

    async def _build_context(self, playwright) -> BrowserContext:
        browser = await playwright.chromium.launch(headless=self.headless)
        return await browser.new_context(
            user_agent=self._random_user_agent(),
            viewport={"width": random.randint(1180, 1490), "height": random.randint(700, 980)},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            proxy=self._proxy_config(),
        )

    def _area_search_url(self, area: str) -> str:
        if self.platform == "zomato":
            return f"https://www.zomato.com/dehradun/{area.lower().replace(' ', '-')}-restaurants"
        if self.platform == "swiggy":
            return f"https://www.swiggy.com/city/dehradun?search={area.replace(' ', '%20')}"
        raise ValueError("Unsupported platform. Use 'zomato' or 'swiggy'.")

    async def discover_restaurants(self, page: Page, area: str) -> list[Restaurant]:
        await page.goto(self._area_search_url(area), wait_until="domcontentloaded", timeout=90000)
        await stealth(page)
        await self._jitter()
        await self._human_like_scroll(page, cycles=12)

        cards = page.locator("a[href*='/dehradun/']") if self.platform == "zomato" else page.locator("a[href*='/restaurants/'], a[href*='/menu']")

        discovered: list[Restaurant] = []
        seen: set[str] = set()
        total = await cards.count()
        for i in range(min(total, self.max_restaurants_per_area * 6)):
            card = cards.nth(i)
            href = await card.get_attribute("href")
            if not href:
                continue
            full_url = href if href.startswith("http") else f"https://www.{self.platform}.com{href}"
            if self.platform == "zomato" and "/dehradun/" not in full_url:
                continue
            if full_url in seen:
                continue
            seen.add(full_url)
            text = (await card.inner_text()).strip() or f"Restaurant-{len(discovered)+1}"
            name = text.split("\n")[0][:120]
            rating = self._parse_rating(text)
            order_url = self._to_order_url(full_url)
            discovered.append(Restaurant(name=name, overall_rating=rating, location_tag=area, source_url=full_url, order_online_url=order_url))
            if len(discovered) >= self.max_restaurants_per_area:
                break

        return discovered

    def _to_order_url(self, url: str) -> str:
        if self.platform == "zomato" and "/order" not in url:
            return url.rstrip("/") + "/order"
        return url

    async def extract_dishes(self, page: Page, restaurant: Restaurant) -> list[DishRecord]:
        if self.platform == "zomato":
            return await self._extract_zomato_dishes(page, restaurant)
        return await self._extract_swiggy_dishes(page, restaurant)

    async def _extract_zomato_dishes(self, page: Page, restaurant: Restaurant) -> list[DishRecord]:
        payloads: list[dict[str, Any]] = []

        async def capture(response: Response) -> None:
            if "getPage" not in response.url:
                return
            if "application/json" not in (response.headers.get("content-type", "") or ""):
                return
            try:
                data = await response.json()
            except Exception:
                return
            if isinstance(data, dict):
                payloads.append(data)

        page.on("response", capture)
        await page.goto(restaurant.order_online_url, wait_until="networkidle", timeout=90000)
        await stealth(page)
        await self._human_like_scroll(page, cycles=8)
        await self._jitter()
        page.remove_listener("response", capture)

        dishes = self._parse_zomato_payloads(payloads, restaurant)
        if not dishes:
            dishes = await self._extract_from_dom(page, restaurant)
        return self._dedupe_dishes(dishes)

    async def _extract_swiggy_dishes(self, page: Page, restaurant: Restaurant) -> list[DishRecord]:
        payloads: list[dict[str, Any]] = []

        async def capture(response: Response) -> None:
            if "/dapi/menu/v4/full" not in response.url:
                return
            try:
                data = await response.json()
            except Exception:
                return
            if isinstance(data, dict):
                payloads.append(data)

        page.on("response", capture)
        await page.goto(restaurant.order_online_url, wait_until="networkidle", timeout=90000)
        await stealth(page)
        await self._human_like_scroll(page, cycles=10)
        await self._jitter()
        page.remove_listener("response", capture)

        dishes = self._parse_swiggy_payloads(payloads, restaurant)
        if not dishes:
            dishes = await self._extract_from_dom(page, restaurant)
        return self._dedupe_dishes(dishes)

    def _parse_zomato_payloads(self, payloads: list[dict[str, Any]], restaurant: Restaurant) -> list[DishRecord]:
        rows: list[DishRecord] = []
        for payload in payloads:
            menus = self._deep_find(payload, ["menu", "items", "dishes", "products"])
            for item in menus:
                name = self._pick(item, ["name", "dish_name", "title"])
                if not name:
                    continue
                price_raw = self._pick(item, ["price", "display_price", "default_price"])
                dish_rating = self._as_float(self._pick(item, ["rating", "rating_value", "aggregate_rating"]))
                dish_votes = self._as_int(self._pick(item, ["rating_count", "ratings_count", "votes", "vote_count"]))
                is_bestseller = bool(self._pick(item, ["is_bestseller", "isBestSeller", "bestseller_tag"]))
                rows.append(self._record(restaurant, name, dish_rating, dish_votes, self._format_price(price_raw), is_bestseller))
        return rows

    def _parse_swiggy_payloads(self, payloads: list[dict[str, Any]], restaurant: Restaurant) -> list[DishRecord]:
        rows: list[DishRecord] = []
        for payload in payloads:
            for item in self._deep_find(payload, ["itemCards", "item_cards", "items"]):
                info = item.get("card", {}).get("info", {}) if isinstance(item, dict) else {}
                source = info if info else item
                name = self._pick(source, ["name"])
                if not name:
                    continue
                price_raw = self._pick(source, ["price", "defaultPrice", "finalPrice"])
                ratings = source.get("ratings", {}) if isinstance(source, dict) else {}
                agg = ratings.get("aggregatedRating", {}) if isinstance(ratings, dict) else {}
                dish_rating = self._as_float(self._pick(agg, ["rating", "ratingValue"]))
                dish_votes = self._as_int(self._pick(agg, ["ratingsCount", "ratingCount", "count"]))
                is_bestseller = bool(self._pick(source, ["isBestseller", "isBestSeller", "is_bestseller"]))
                rows.append(self._record(restaurant, name, dish_rating, dish_votes, self._format_price(price_raw), is_bestseller))
        return rows

    def _record(
        self,
        restaurant: Restaurant,
        dish_name: str,
        dish_rating: float | None,
        dish_votes: int | None,
        price: str | None,
        is_bestseller: bool,
    ) -> DishRecord:
        return DishRecord(
            platform=self.platform,
            restaurant_name=restaurant.name,
            overall_rating=restaurant.overall_rating,
            dish_name=dish_name.strip(),
            dish_rating=dish_rating,
            dish_votes=dish_votes,
            price=price,
            location_tag=restaurant.location_tag,
            is_bestseller=is_bestseller,
            high_potential_item=self._is_high_potential(restaurant.overall_rating, dish_rating, dish_votes),
            market_score=self._market_score(dish_rating, dish_votes),
        )

    async def _extract_from_dom(self, page: Page, restaurant: Restaurant) -> list[DishRecord]:
        rows = page.locator("div:has-text('₹')")
        parsed: list[DishRecord] = []
        for i in range(min(await rows.count(), 500)):
            text = (await rows.nth(i).inner_text()).strip()
            price_match = re.search(r"₹\s?\d+(?:\.\d{1,2})?", text)
            if not text or not price_match:
                continue
            dish_name = text.split("\n")[0].strip()
            if len(dish_name) < 2:
                continue
            dish_rating = self._parse_inline_rating(text)
            dish_votes = self._parse_votes(text)
            parsed.append(
                self._record(
                    restaurant,
                    dish_name,
                    dish_rating,
                    dish_votes,
                    price_match.group(0),
                    bool(re.search(r"best\s*seller", text, flags=re.IGNORECASE)),
                )
            )
        return parsed

    @staticmethod
    def _deep_find(payload: Any, keys: list[str]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    if k in keys and isinstance(v, list):
                        for obj in v:
                            if isinstance(obj, dict):
                                found.append(obj)
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(payload)
        return found

    @staticmethod
    def _pick(data: Any, keys: list[str]) -> Any:
        if not isinstance(data, dict):
            return None
        for k in keys:
            if k in data and data[k] not in (None, ""):
                return data[k]
        return None

    @staticmethod
    def _format_price(value: Any) -> str | None:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            amount = float(value)
            if amount > 1000:
                amount /= 100
            return f"₹{amount:.0f}"
        text = str(value)
        if text.startswith("₹"):
            return text
        return f"₹{text}"

    @staticmethod
    def _parse_rating(text: str) -> float | None:
        match = re.search(r"\b([0-5](?:\.\d)?)\b", text)
        if not match:
            return None
        return DehradunDishIntelAgent._as_float(match.group(1))

    @staticmethod
    def _parse_inline_rating(text: str) -> float | None:
        match = re.search(r"(?:rating|★)?\s*([0-5](?:\.\d)?)", text, flags=re.IGNORECASE)
        return DehradunDishIntelAgent._as_float(match.group(1)) if match else None

    @staticmethod
    def _parse_votes(text: str) -> int | None:
        m = re.search(r"(\d+)\s*(?:votes?|ratings?)", text, flags=re.IGNORECASE)
        if m:
            return int(m.group(1))
        m2 = re.search(r"\((\d+)\)", text)
        return int(m2.group(1)) if m2 else None

    @staticmethod
    def _as_float(value: Any) -> float | None:
        try:
            out = float(value)
            return out if 0 <= out <= 5 else out
        except Exception:
            return None

    @staticmethod
    def _as_int(value: Any) -> int | None:
        if value is None:
            return None
        text = str(value).replace(",", "")
        try:
            return int(float(text))
        except Exception:
            return None

    @staticmethod
    def _is_high_potential(overall: float | None, dish_rating: float | None, votes: int | None) -> bool:
        return bool(overall is not None and dish_rating is not None and votes is not None and overall < 4.0 and dish_rating > 4.5 and votes > 10)

    @staticmethod
    def _market_score(dish_rating: float | None, votes: int | None) -> float | None:
        if dish_rating is None or votes is None:
            return None
        return round(dish_rating * math.log10(votes + 1), 4)

    @staticmethod
    def _dedupe_dishes(items: list[DishRecord]) -> list[DishRecord]:
        seen: set[tuple[str, str, str | None]] = set()
        out: list[DishRecord] = []
        for item in items:
            key = (item.restaurant_name.lower(), item.dish_name.lower(), item.price)
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    async def run(self, areas: list[str]) -> list[DishRecord]:
        rows: list[DishRecord] = []
        async with async_playwright() as p:
            context = await self._build_context(p)
            page = await context.new_page()
            for area in areas:
                restaurants = await self.discover_restaurants(page, area)
                for restaurant in restaurants:
                    try:
                        rows.extend(await self.extract_dishes(page, restaurant))
                    except Exception as exc:
                        print(f"[WARN] Skipping {restaurant.name}: {exc}")
                    await self._jitter()
            await context.close()
        return rows

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
                    "platform",
                    "restaurant_name",
                    "overall_rating",
                    "dish_name",
                    "dish_rating",
                    "dish_votes",
                    "price",
                    "location_tag",
                    "is_bestseller",
                    "high_potential_item",
                    "market_score",
                ],
            )
            writer.writeheader()
            writer.writerows(payload)

        return json_path, csv_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract dish-level intelligence for Dehradun.")
    parser.add_argument("--platform", default="zomato", choices=["zomato", "swiggy"])
    parser.add_argument("--areas", nargs="*", default=DEHRADUN_AREAS)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--min-delay", type=float, default=0.7)
    parser.add_argument("--max-delay", type=float, default=2.2)
    parser.add_argument("--max-restaurants-per-area", type=int, default=100)
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
