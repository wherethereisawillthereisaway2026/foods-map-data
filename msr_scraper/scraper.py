#!/usr/bin/env python3
"""
MSR (ミステリーショッピングリサーチ) 飲食モニター スクレイパー
ログイン → 都道府県×ページ を全件取得 → ジオコーディング
Usage: python scraper.py
Output: msr-data.json
"""
import json, os, re, sys, time, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

LOGON_URL   = "https://www2.ms-r.com/MSR/MonitorNew/logon.asp?EntryType="
API_URL     = "https://www2.ms-r.com/MSRP/Monitor/Api/GetInvitationForSearch.php"
HEADERS     = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
LOGIN_ID    = os.environ["MSR_LOGIN_ID"]
LOGIN_PW    = os.environ["MSR_LOGIN_PW"]
GSI_GEO_URL = "https://msearch.gsi.go.jp/address-search/AddressSearch"
OUTPUT      = Path(__file__).parent.parent / "msr-data.json"
SLEEP_REQ   = 0.5
SLEEP_GEO   = 0.3


def login() -> requests.Session:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()
        # AutomationControlledフラグをJSで除去
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page.goto(LOGON_URL, wait_until="networkidle", timeout=30000)
        page.wait_for_selector('input[name="LogonID"]', timeout=20000)
        page.fill('input[name="LogonID"]', LOGIN_ID)
        page.fill('#Password', LOGIN_PW)
        page.get_by_role("button", name="モニターサイトへ ログイン").click()
        page.wait_for_url("**/MSRP/Monitor/**", timeout=30000)
        cookies = ctx.cookies()
        browser.close()
    s = requests.Session()
    s.headers.update(HEADERS)
    for c in cookies:
        s.cookies.set(c["name"], c["value"], domain=c.get("domain", "").lstrip("."))
    print("ログイン完了", file=sys.stderr)
    return s


def build_params(pref_id: int, page: int) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    return {
        "PrefectureID": str(pref_id), "AreaAll": "1", "GyosyuID[1]": "1",
        "StartDate": today, "EndDate": "2026-12-31",
        "Youbi[1]": "1", "Youbi[2]": "1", "Youbi[3]": "1", "Youbi[4]": "1",
        "Youbi[5]": "1", "Youbi[6]": "1", "Youbi[7]": "1", "Youbi[8]": "1",
        "Time[Specification]": "0", "BrandShopName": "", "BrandShopInclude": "1",
        "Page": str(page), "Order": "4", "ViewType": "1", "NoLinkCompany": "",
    }


def parse_cards(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    stores = []
    for card in soup.select("div.d-flex.flex-column.gap-2.border-top"):
        name_el = card.select_one(".p-search-result__list-shopname span")
        if not name_el:
            continue
        name = name_el.get_text(strip=True)
        map_el = card.select_one("a[onclick*='openMapModal']")
        address = ""
        if map_el:
            m = re.search(r"openMapModal\(['\"].*?['\"],\s*['\"](.*?)['\"]\)", map_el.get("onclick", ""))
            if m:
                address = m.group(1)
        reward_el = card.select_one(".p-result-detail__price")
        reward = reward_el.get_text(strip=True) if reward_el else ""
        inv_el = card.select_one("[data-invitation]")
        inv_id = inv_el["data-invitation"] if inv_el else ""
        if not address:
            continue
        stores.append({"name": name, "address": address, "reward": reward, "inv_id": inv_id})
    return stores


def fetch_prefecture(session: requests.Session, pref_id: int) -> list[dict]:
    stores, page = [], 1
    while True:
        r = session.post(API_URL, data=build_params(pref_id, page), timeout=30)
        r.raise_for_status()
        cards = parse_cards(r.text)
        stores.extend(cards)
        if "js-scroll-verical" not in r.text or not cards:
            break
        page += 1
        time.sleep(SLEEP_REQ)
    return stores


def geocode(_name: str, address: str) -> tuple[float | None, float | None]:
    params = urllib.parse.urlencode({"q": address})
    try:
        req = urllib.request.Request(f"{GSI_GEO_URL}?{params}", headers={"User-Agent": "Mozilla/5.0"})
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        if data:
            c = data[0]["geometry"]["coordinates"]
            return c[1], c[0]
    except Exception:
        pass
    return None, None


def main():
    session = login()
    all_raw: list[dict] = []
    for pref_id in range(1, 48):
        stores = fetch_prefecture(session, pref_id)
        all_raw.extend(stores)
        print(f"  pref={pref_id:2d}: {len(stores):3d}件 / 累計 {len(all_raw)}", file=sys.stderr)
        time.sleep(SLEEP_REQ)

    seen, unique = set(), []
    for s in all_raw:
        key = s["inv_id"] or f"{s['name']}|{s['address']}"
        if key not in seen:
            seen.add(key)
            unique.append(s)
    print(f"\nユニーク: {len(unique)}件", file=sys.stderr)

    stores_out = []
    for i, s in enumerate(unique):
        lat, lng = geocode(s["name"], s["address"])
        if lat is None:
            continue
        stores_out.append({
            "name": s["name"], "address": s["address"],
            "lat": lat, "lng": lng,
            "reward": s["reward"], "budget": "",
            "url": "https://www2.ms-r.com/MSRP/Monitor/Search/Search.php",
        })
        if (i + 1) % 100 == 0:
            print(f"  geocode {i+1}/{len(unique)}", file=sys.stderr)
        time.sleep(SLEEP_GEO)

    out = {"stores": stores_out, "updated": datetime.now(timezone(timedelta(hours=9))).isoformat()}
    OUTPUT.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"\n完了: {len(stores_out)}件 → {OUTPUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
