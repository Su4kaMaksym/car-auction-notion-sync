#!/usr/bin/env python3
"""Синхронізація куплених авто з car-auctions API в Notion.

Для кожної покупки, створеної в кабінеті не раніше SINCE, і якої ще немає
в базі Notion (перевірка за VIN), створює рядок лише з трьома полями:
  - «Автомобіль»  — назва авто з аукціону (рік, марка, модель)
  - «VIN-код»     — VIN
  - «Місяць»      — місяць покупки (за київським часом)

Змінні середовища:
  AUCTION_API_KEY   ключ API аукціону (обов'язково)
  NOTION_TOKEN      токен інтеграції Notion (обов'язково)
  SINCE             дата у форматі YYYY-MM-DD, з якої брати покупки (обов'язково)
  AUCTION_BASE_URL  за замовчуванням https://integration.car-auctions.com.ua
  NOTION_DB_ID      за замовчуванням база «Автомобілі — копія для API»
  DRY_RUN=1         лише показати, що буде додано, нічого не записувати
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BASE_URL = os.environ.get("AUCTION_BASE_URL", "https://integration.car-auctions.com.ua").rstrip("/")
DB_ID = os.environ.get("NOTION_DB_ID", "416ce9d6-871f-81f4-a93b-ef454ff2a38d")
KYIV = ZoneInfo("Europe/Kyiv")
MONTHS = ["Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
          "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень"]

# Назви колонок у базі Notion
P_CAR, P_VIN, P_MONTH = "Автомобіль", "VIN-код", "Місяць"


def http(url, headers, body=None, method=None):
    headers = {"User-Agent": "car-sync/1.0", "Accept": "application/json", **headers}
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(5):
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method or ("POST" if data else "GET"))
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(2 ** attempt * 2)
                continue
            raise RuntimeError(f"{method or 'GET'} {url.split('?')[0]} -> {e.code}: {e.read()[:500].decode(errors='replace')}")


def fetch_purchases(api_key, since):
    """Усі покупки (свої й субдилерів), створені з дати since."""
    out, skip = [], 0
    while True:
        q = urllib.parse.urlencode({
            "createdAt[$gte]": since,
            "$sort[createdAt]": 1,
            "$limit": 50,
            "$skip": skip,
        })
        page = http(f"{BASE_URL}/purchases?{q}", {"x-api-key": api_key})
        out.extend(page["data"])
        skip += 50
        if skip >= page["total"] or not page["data"]:
            return out


def notion(token):
    return {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"}


def existing_vins(token):
    vins, cursor = set(), None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        d = http(f"https://api.notion.com/v1/databases/{DB_ID}/query", notion(token), body)
        for row in d["results"]:
            v = "".join(t["plain_text"] for t in row["properties"][P_VIN]["rich_text"])
            if v.strip():
                vins.add(v.strip().upper())
        if not d["has_more"]:
            return vins
        cursor = d["next_cursor"]


def month_label(p):
    """Назва місяця як у колонці «Місяць»: «Листопад 26»; 2025 р. — без року."""
    raw = p.get("date") or p.get("createdAt")
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(KYIV)
    if dt.year < 2025 or (dt.year == 2025 and dt.month < 7):
        return "Попередні місяці"
    if dt.year == 2025:
        return MONTHS[dt.month - 1]
    return f"{MONTHS[dt.month - 1]} {dt.year % 100:02d}"


def car_name(p):
    name = (p.get("title") or "").strip()
    if not name:
        name = " ".join(x for x in (p.get("year"), p.get("make"), p.get("model")) if x)
    return name or "Без назви"


def create_row(token, p):
    body = {
        "parent": {"database_id": DB_ID},
        "properties": {
            P_CAR: {"title": [{"text": {"content": car_name(p)}}]},
            P_VIN: {"rich_text": [{"text": {"content": p["vin"].strip().upper()}}]},
            P_MONTH: {"select": {"name": month_label(p)}},
        },
    }
    http("https://api.notion.com/v1/pages", notion(token), body)


def main():
    api_key, token, since = (os.environ.get(k) for k in ("AUCTION_API_KEY", "NOTION_TOKEN", "SINCE"))
    if not (api_key and token and since):
        sys.exit("Потрібні змінні AUCTION_API_KEY, NOTION_TOKEN і SINCE (YYYY-MM-DD)")
    dry = os.environ.get("DRY_RUN") == "1"

    purchases = fetch_purchases(api_key, since)
    have = existing_vins(token)
    added = 0
    for p in purchases:
        vin = (p.get("vin") or "").strip().upper()
        if not vin or vin in have:
            continue
        # Логи публічні, тому без назви авто й VIN
        if dry:
            print(f"[DRY] + {car_name(p)} | {vin} | {month_label(p)}")
        else:
            create_row(token, p)
        have.add(vin)
        added += 1
    print(f"Покупок з {since}: {len(purchases)}; додано нових: {added}")


if __name__ == "__main__":
    main()
