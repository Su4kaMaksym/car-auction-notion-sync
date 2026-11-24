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
  NOTION_DB_ID      ID баз через кому; за замовчуванням «Автомобілі» і «Автомобілі — копія для API»
  Дата покупки й контейнер з аукціону за VIN пишуться в DETAIL_TARGETS:
  «Автомобілі» (приховані колонки) і «Логістика авто» (ручні дати не перезаписуються)
  DRY_RUN=1         лише показати, що буде додано, нічого не записувати
"""
from http.client import HTTPException
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
DB_IDS = [x.strip() for x in os.environ.get(
    "NOTION_DB_ID",
    "20970ca2-4c28-49cd-83f1-ebee898a6b81,"   # Автомобілі (основна)
    "416ce9d6-871f-81f4-a93b-ef454ff2a38d",   # Автомобілі — копія для API
).split(",") if x.strip()]
# (назва в логах, база, колонка дати, колонка контейнера, чи перезаписувати вже заповнену дату)
DETAIL_TARGETS = [
    ("Автомобілі", "20970ca2-4c28-49cd-83f1-ebee898a6b81", "Дата покупки", "Контейнер", True),
    ("Логістика авто", "418ce9d6-871f-816e-9cf8-f046a43e1e0f", "ДАТА ПОКУПКИ", "Номер контейнера", False),
]
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
        except (urllib.error.URLError, HTTPException, ConnectionError, TimeoutError, ValueError):
            # обрив з'єднання / неповна відповідь — пробуємо ще раз
            # (крім створення рядка: повтор міг би дати дубль)
            if attempt < 4 and not url.endswith("/v1/pages"):
                time.sleep(2 ** attempt * 2)
                continue
            raise


def fetch_purchases(api_key, since=None):
    """Покупки (свої й субдилерів), створені з дати since (або всі); субдилерів відсікає main()."""
    out, skip = [], 0
    while True:
        params = {"$sort[createdAt]": 1, "$limit": 50, "$skip": skip}
        if since:
            params["createdAt[$gte]"] = since
        q = urllib.parse.urlencode(params)
        page = http(f"{BASE_URL}/purchases?{q}", {"x-api-key": api_key})
        out.extend(page["data"])
        skip += 50
        if skip >= page["total"] or not page["data"]:
            return out


def notion(token):
    return {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"}


def existing_vins(token, db_id):
    vins, cursor = set(), None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        d = http(f"https://api.notion.com/v1/databases/{db_id}/query", notion(token), body)
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


def kyiv_date(p):
    """Дата покупки (YYYY-MM-DD) за київським часом."""
    raw = p.get("date") or p.get("createdAt")
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KYIV).date().isoformat()


def text_of(prop):
    return "".join(t["plain_text"] for t in (prop or {}).get("rich_text", [])).strip()


def query_all(token, db_id):
    rows, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        d = http(f"https://api.notion.com/v1/databases/{db_id}/query", notion(token), body)
        rows += d["results"]
        if not d["has_more"]:
            return rows
        cursor = d["next_cursor"]


def sync_details(token, name, db_id, p_bought, p_container, overwrite_date, purchases, dry):
    """Ставить дату покупки й номер контейнера з аукціону в рядки бази (за VIN).
    Контейнер лише дописується/оновлюється, але не стирається (після прибуття API його не віддає)."""
    db = http(f"https://api.notion.com/v1/databases/{db_id}", notion(token))
    missing = {k: v for k, v in ((p_bought, {"date": {}}), (p_container, {"rich_text": {}}))
               if k not in db["properties"]}
    if missing and not dry:
        http(f"https://api.notion.com/v1/databases/{db_id}", notion(token),
             {"properties": missing}, method="PATCH")
    by_vin = {p["vin"].strip().upper(): p for p in purchases if p.get("vin")}
    changed = 0
    for row in query_all(token, db_id):
        props = row["properties"]
        p = by_vin.get(text_of(props.get(P_VIN)).upper())
        if not p:
            continue
        upd = {}
        bought = kyiv_date(p)
        cur = ((props.get(p_bought) or {}).get("date") or {}).get("start")
        if cur != bought and (overwrite_date or not cur):
            upd[p_bought] = {"date": {"start": bought}}
        cont = ((p.get("currentDeliveryStatus") or {}).get("container_number") or "").strip()
        if cont and text_of(props.get(p_container)) != cont:
            upd[p_container] = {"rich_text": [{"text": {"content": cont}}]}
        if not upd:
            continue
        if dry:
            print(f"[DRY] {name}: {text_of(props.get(P_VIN))} -> {list(upd)}")
        else:
            http(f"https://api.notion.com/v1/pages/{row['id']}", notion(token),
                 {"properties": upd}, method="PATCH")
        changed += 1
    print(f"{name}: оновлено дату/контейнер у рядках: {changed}")


def car_name(p):
    name = (p.get("title") or "").strip()
    if not name:
        name = " ".join(x for x in (p.get("year"), p.get("make"), p.get("model")) if x)
    return name or "Без назви"


def create_row(token, db_id, p):
    body = {
        "parent": {"database_id": db_id},
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

    all_purchases = fetch_purchases(api_key)
    purchases = [p for p in all_purchases if p.get("createdAt", "") >= since]
    for n, db_id in enumerate(DB_IDS, 1):
        have = existing_vins(token, db_id)
        added = 0
        for p in purchases:
            vin = (p.get("vin") or "").strip().upper()
            if not vin or vin in have:
                continue
            if p.get("ownership") == "sub_dealer":   # авто субдилера (Західавто) не додаємо
                continue
            # Логи публічні, тому без назви авто й VIN
            if dry:
                print(f"[DRY] база {n}: + {car_name(p)} | {vin} | {month_label(p)}")
            else:
                create_row(token, db_id, p)
            have.add(vin)
            added += 1
        print(f"База {n}: покупок з {since}: {len(purchases)}; додано нових: {added}")

    for target in DETAIL_TARGETS:
        sync_details(token, *target, all_purchases, dry)


if __name__ == "__main__":
    main()
