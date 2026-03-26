#!/usr/bin/env python3
"""Unified food intelligence agent for Dehradun (Zomato + Swiggy)."""
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

from dotenv import load_dotenv
from fake_useragent import UserAgent
from playwright.async_api import BrowserContext, Page, Response, async_playwright
from playwright_stealth import stealth

DEHRADUN_AREAS = ["Jakhan", "Race Course", "Rajpur Road"]
DEFAULT_OUTPUT_DIR = Path("output")
DEFAULT_CSV_NAME = "master_market_report.csv"

MAC_CHROME_UA_FALLBACKS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]


@dataclass
class Restaurant:
    platform: str
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
    price: str | None
    dish_rating: float | None
    dish_votes: int | None
    is_bestseller: bool
    location_tag: str
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
        retries: int,
    ) -> None:
        self.platform = platform.lower().strip()
        self.headless = headless
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.max_restaurants_per_area = max_restaurants_per_area
        self.output_dir = output_dir
        self.retries = retries
        self.ua_provider = UserAgent(browsers=["chrome", "edge", "firefox", "safari"])

    def _proxy_pool(self) -> list[str]:
        raw = os.getenv("PROXY_URL", "").strip()
        if not raw:
            return []
        return [x.strip() for x in raw.split(",") if x.strip()]

    def _pick_proxy(self) -> dict[str, str] | None:
        proxies = self._proxy_pool()
        if not proxies:
            return None
        return {"server": random.choice(proxies)}

    def _random_user_agent(self) -> str:
        try:
            ua = self.ua_provider.random
            return ua if "Macintosh" in ua else random.choice(MAC_CHROME_UA_FALLBACKS)
        except Exception:
            return random.choice(MAC_CHROME_UA_FALLBACKS)

    async def _jitter(self, lower: float | None = None, upper: float | None = None) -> None:
        await asyncio.sleep(random.uniform(lower or self.min_delay, upper or self.max_delay))

    async def _human_like_scroll(self, page: Page, cycles: int = 10) -> None:
        for _ in range(cycles):
            await page.mouse.wheel(0, random.randint(300, 950))
            await self._jitter()

    def _browser_headers(self, referer: str) -> dict[str, str]:
        return {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "max-age=0",
            "Pragma": "no-cache",
            "Referer": referer,
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
        }

    async def _build_context(self, playwright, referer: str) -> BrowserContext:
        browser = await playwright.chromium.launch(
            headless=self.headless,
            channel="chrome",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        context = await browser.new_context(
            user_agent=self._random_user_agent(),
            viewport={"width": 1512, "height": 982},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            color_scheme="light",
            java_script_enabled=True,
            ignore_https_errors=False,
            proxy=self._pick_proxy(),
            extra_http_headers=self._browser_headers(referer),
        )
        return context

    def _area_search_url(self, area: str) -> str:
        if self.platform == "zomato":
            return f"https://www.zomato.com/dehradun/{area.lower().replace(' ', '-')}-restaurants"
        if self.platform == "swiggy":
            return f"https://www.swiggy.com/city/dehradun?search={area.replace(' ', '%20')}"
        raise ValueError("Unsupported platform. Use 'zomato' or 'swiggy'.")

    async def _goto_with_retry(self, page: Page, url: str, wait_until: str = "domcontentloaded") -> None:
        backoff = 1.4
        for attempt in range(1, self.retries + 1):
            try:
                await page.goto(url, wait_until=wait_until, timeout=90000)
                return
            except Exception as exc:
                msg = str(exc)
                if attempt == self.retries:
                    raise
                if "ERR_HTTP2_PROTOCOL_ERROR" in msg:
                    await self._jitter(2.0, 4.0)
                else:
                    await self._jitter(backoff, backoff + 1.2)
                backoff = min(backoff * 1.7, 6.0)

    async def discover_restaurants(self, page: Page, area: str) -> list[Restaurant]:
        url = self._area_search_url(area)
        await self._goto_with_retry(page, url)
        await stealth(page)
        await self._jitter()
        await self._human_like_scroll(page, cycles=12)

        cards = page.locator("a[href*='/dehradun/']") if self.platform == "zomato" else page.locator("a[href*='/restaurants/'], a[href*='/menu']")

        discovered: list[Restaurant] = []
        seen: set[str] = set()
        total = await cards.count()

        for i in range(min(total, self.max_restaurants_per_area * 8)):
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
            name = text.split("\n")[0].strip()[:120]
            overall = self._parse_rating(text)
            discovered.append(
                Restaurant(
                    platform=self.platform,
                    name=name,
                    overall_rating=overall,
                    location_tag=area,
                    source_url=full_url,
                    order_online_url=self._to_order_url(full_url),
                )
            )
            if len(discovered) >= self.max_restaurants_per_area:
                break

        return discovered

    def _to_order_url(self, url: str) -> str:
        if self.platform == "zomato" and "/order" not in url:
            return url.rstrip("/") + "/order"
        return url

    async def extract_dishes(self, page: Page, restaurant: Restaurant) -> list[DishRecord]:
        if restaurant.platform == "zomato":
            return await self._extract_zomato_dishes(page, restaurant)
        return await self._extract_swiggy_dishes(page, restaurant)

    async def _extract_zomato_dishes(self, page: Page, restaurant: Restaurant) -> list[DishRecord]:
        payloads: list[dict[str, Any]] = []

        async def capture(resp: Response) -> None:
            if "getPage" not in resp.url:
                return
            if "application/json" not in (resp.headers.get("content-type", "") or ""):
                return
            try:
                data = await resp.json()
            except Exception:
                return
            if isinstance(data, dict):
                payloads.append(data)

        page.on("response", capture)
        try:
            await self._goto_with_retry(page, restaurant.order_online_url, wait_until="networkidle")
            await stealth(page)
            await self._human_like_scroll(page, cycles=8)
            await self._jitter()
        finally:
            page.remove_listener("response", capture)

        rows = self._parse_zomato_payloads(payloads, restaurant)
        return self._dedupe(rows or await self._extract_from_dom(page, restaurant))

    async def _extract_swiggy_dishes(self, page: Page, restaurant: Restaurant) -> list[DishRecord]:
        payloads: list[dict[str, Any]] = []

        async def capture(resp: Response) -> None:
            if "/dapi/menu/v4/full" not in resp.url:
                return
            try:
                data = await resp.json()
            except Exception:
                return
            if isinstance(data, dict):
                payloads.append(data)

        page.on("response", capture)
        try:
            await self._goto_with_retry(page, restaurant.order_online_url, wait_until="networkidle")
            await stealth(page)
            await self._human_like_scroll(page, cycles=10)
            await self._jitter()
        finally:
            page.remove_listener("response", capture)

        rows = self._parse_swiggy_payloads(payloads, restaurant)
        return self._dedupe(rows or await self._extract_from_dom(page, restaurant))

    def _parse_zomato_payloads(self, payloads: list[dict[str, Any]], restaurant: Restaurant) -> list[DishRecord]:
        rows: list[DishRecord] = []
        for payload in payloads:
            for item in self._deep_find(payload, ["menu", "items", "dishes", "products"]):
                name = self._pick(item, ["name", "dish_name", "title"])
                if not name:
                    continue
                dish_rating = self._as_float(self._pick(item, ["rating", "rating_value", "aggregate_rating"]))
                dish_votes = self._as_int(self._pick(item, ["rating_count", "ratings_count", "votes", "vote_count"]))
                price = self._format_price(self._pick(item, ["price", "display_price", "default_price"]))
                bestseller = bool(self._pick(item, ["is_bestseller", "isBestSeller", "bestseller_tag"]))
                rows.append(self._to_record(restaurant, str(name), price, dish_rating, dish_votes, bestseller))
        return rows

    def _parse_swiggy_payloads(self, payloads: list[dict[str, Any]], restaurant: Restaurant) -> list[DishRecord]:
        rows: list[DishRecord] = []
        for payload in payloads:
            for item in self._deep_find(payload, ["itemCards", "item_cards", "items"]):
                info = item.get("card", {}).get("info", {}) if isinstance(item, dict) else {}
                src = info if info else item
                name = self._pick(src, ["name"])
                if not name:
                    continue
                price = self._format_price(self._pick(src, ["price", "defaultPrice", "finalPrice"]))
                agg = (src.get("ratings", {}) or {}).get("aggregatedRating", {}) if isinstance(src, dict) else {}
                dish_rating = self._as_float(self._pick(agg, ["rating", "ratingValue"]))
                dish_votes = self._as_int(self._pick(agg, ["ratingsCount", "ratingCount", "count"]))
                bestseller = bool(self._pick(src, ["isBestseller", "isBestSeller", "is_bestseller"]))
                rows.append(self._to_record(restaurant, str(name), price, dish_rating, dish_votes, bestseller))
        return rows

    def _to_record(
        self,
        restaurant: Restaurant,
        dish_name: str,
        price: str | None,
        dish_rating: float | None,
        dish_votes: int | None,
        is_bestseller: bool,
    ) -> DishRecord:
        return DishRecord(
            platform=restaurant.platform,
            restaurant_name=restaurant.name,
            overall_rating=restaurant.overall_rating,
            dish_name=dish_name.strip(),
            price=price,
            dish_rating=dish_rating,
            dish_votes=dish_votes,
            is_bestseller=is_bestseller,
            location_tag=restaurant.location_tag,
            high_potential_item=self._is_high_potential(restaurant.overall_rating, dish_rating, dish_votes),
            market_score=self._market_score(dish_rating, dish_votes),
        )

    async def _extract_from_dom(self, page: Page, restaurant: Restaurant) -> list[DishRecord]:
        nodes = page.locator("div:has-text('₹')")
        rows: list[DishRecord] = []
        for i in range(min(await nodes.count(), 500)):
            text = (await nodes.nth(i).inner_text()).strip()
            if not text:
                continue
            price_match = re.search(r"₹\s?\d+(?:\.\d{1,2})?", text)
            if not price_match:
                continue
            dish_name = text.split("\n")[0].strip()
            if len(dish_name) < 2:
                continue
            dish_rating = self._parse_inline_rating(text)
            dish_votes = self._parse_votes(text)
            bestseller = bool(re.search(r"best\s*seller", text, flags=re.IGNORECASE))
            rows.append(self._to_record(restaurant, dish_name, price_match.group(0), dish_rating, dish_votes, bestseller))
        return rows

    @staticmethod
    def _deep_find(data: Any, target_keys: list[str]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for key, val in node.items():
                    if key in target_keys and isinstance(val, list):
                        result.extend([x for x in val if isinstance(x, dict)])
                    walk(val)
            elif isinstance(node, list):
                for val in node:
                    walk(val)

        walk(data)
        return result

    @staticmethod
    def _pick(data: Any, keys: list[str]) -> Any:
        if not isinstance(data, dict):
            return None
        for key in keys:
            if key in data and data[key] not in (None, ""):
                return data[key]
        return None

    @staticmethod
    def _format_price(raw: Any) -> str | None:
        if raw in (None, ""):
            return None
        if isinstance(raw, (int, float)):
            val = float(raw)
            if val >= 1000:
                val = val / 100
            return f"₹{val:.0f}"
        text = str(raw)
        return text if text.startswith("₹") else f"₹{text}"

    @staticmethod
    def _as_float(val: Any) -> float | None:
        try:
            return float(val)
        except Exception:
            return None

    @staticmethod
    def _as_int(val: Any) -> int | None:
        if val is None:
            return None
        try:
            return int(float(str(val).replace(",", "")))
        except Exception:
            return None

    @staticmethod
    def _parse_rating(text: str) -> float | None:
        m = re.search(r"\b([0-5](?:\.\d)?)\b", text)
        return DehradunDishIntelAgent._as_float(m.group(1)) if m else None

    @staticmethod
    def _parse_inline_rating(text: str) -> float | None:
        m = re.search(r"(?:rating|★)?\s*([0-5](?:\.\d)?)", text, flags=re.IGNORECASE)
        return DehradunDishIntelAgent._as_float(m.group(1)) if m else None

    @staticmethod
    def _parse_votes(text: str) -> int | None:
        m = re.search(r"(\d+)\s*(?:votes?|ratings?)", text, flags=re.IGNORECASE)
        if m:
            return int(m.group(1))
        m2 = re.search(r"\((\d+)\)", text)
        return int(m2.group(1)) if m2 else None

    @staticmethod
    def _is_high_potential(overall: float | None, dish_rating: float | None, votes: int | None) -> bool:
        return bool(overall is not None and dish_rating is not None and votes is not None and overall < 4.0 and dish_rating > 4.5 and votes > 10)

    @staticmethod
    def _market_score(dish_rating: float | None, dish_votes: int | None) -> float | None:
        if dish_rating is None or dish_votes is None:
            return None
        return round(dish_rating * math.log10(dish_votes + 1), 4)

    @staticmethod
    def _dedupe(rows: list[DishRecord]) -> list[DishRecord]:
        seen: set[tuple[str, str, str | None, str]] = set()
        out: list[DishRecord] = []
        for row in rows:
            key = (row.platform, row.restaurant_name.lower(), row.dish_name.lower(), row.price)
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
        return out

    async def run(self, areas: list[str]) -> list[DishRecord]:
        all_rows: list[DishRecord] = []
        async with async_playwright() as p:
            for area in areas:
                referer = self._area_search_url(area)
                context = await self._build_context(p, referer=referer)
                page = await context.new_page()
                try:
                    restaurants = await self.discover_restaurants(page, area)
                    for restaurant in restaurants:
                        try:
                            all_rows.extend(await self.extract_dishes(page, restaurant))
                        except Exception as exc:
                            print(f"[WARN] Failed restaurant {restaurant.name}: {exc}")
                        await self._jitter()
                finally:
                    await context.close()
        return self._dedupe(all_rows)

    def save(self, rows: list[DishRecord]) -> tuple[Path, Path]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        json_path = self.output_dir / f"dehradun_master_database_{ts}.json"
        csv_path = self.output_dir / DEFAULT_CSV_NAME

        payload = [asdict(row) for row in rows]
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
                    "price",
                    "dish_rating",
                    "dish_votes",
                    "is_bestseller",
                    "location_tag",
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
    parser.add_argument("--max-delay", type=float, default=2.3)
    parser.add_argument("--max-restaurants-per-area", type=int, default=100)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--retries", type=int, default=4)
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
        retries=args.retries,
    )
    rows = await agent.run(args.areas)
    json_path, csv_path = agent.save(rows)
    print(f"Saved {len(rows)} rows")
    print(f"JSON: {json_path}")
    print(f"CSV:  {csv_path}")


if __name__ == "__main__":
    asyncio.run(main())
