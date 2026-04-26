#!/usr/bin/env python3
"""
Fancrew グルメモニター スクレイパー
ログイン不要 — REST API で全件取得
Usage: python fancrew_scraper/scraper.py
Output: fancrew-data.json (同リポジトリルート)
"""
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

API_URL   = "https://www.fancrew.jp/api/toc-search-v2"
HEADERS   = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Referer": "https://www.fancrew.jp/search/result/1",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}
SEARCH_COUNT = 20
OUTPUT = Path(__file__).parent.parent / "fancrew-data.json"


def fetch_page(offset: int) -> dict:
    payload = {
        "prefectureIds": [], "areaIds": [], "categoryId": 1,
        "distance": None, "freeWord": "", "genreIds": [],
        "latitude": None, "longitude": None,
        "onlyChildAccompaniedFlg": None, "onlySoloVisitFlg": None,
        "rewardType": None, "rewardUpFlg": False, "serveFood": None,
        "stationIds": [], "visitDay": "", "visitTime": None,
        "offset": offset, "sort": 0,
        "currentLocationSearchFlg": False,
        "searchCount": SEARCH_COUNT, "cityCodes": [],
    }
    resp = requests.post(API_URL, json=payload, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    return resp.json()


def scrape_all() -> list[dict]:
    stores = []
    offset = 0
    total = None

    while True:
        data = fetch_page(offset)
        if total is None:
            total = data.get("allSearchNum", 0)
            print(f"総件数: {total}", file=sys.stderr)

        monitors = data.get("monitorList", [])
        if not monitors:
            break

        valid = []
        ended = 0
        for m in monitors:
            shop = m["shop"]
            # 有効案件のみ（キャンセル待ちは除外）
            if not m.get("canApplyFlg"):
                ended += 1
                continue
            lat = shop.get("latitude")
            lng = shop.get("longitude")
            if not lat or not lng:
                continue
            # 予算テキストから数値を抽出
            budget_str = m.get("shopBudget", "")
            valid.append({
                "name":    shop["name"],
                "address": shop.get("address", ""),
                "lat":     lat,
                "lng":     lng,
                "reward":  m.get("reward", ""),
                "budget":  budget_str,
                "url":     f"https://www.fancrew.jp/detail2/{m['monitorBaseId']}?categoryId=1&classicFlg=false",
            })

        stores.extend(valid)
        print(
            f"  offset={offset:4d}: 有効 {len(valid)}, 終了 {ended} / 累計 {len(stores)}",
            file=sys.stderr,
        )

        # 終了案件が過半数を超えたら残りは全部終了と判断して打ち切り
        if ended > len(monitors) // 2:
            print("  → 終了案件多数。打ち切り", file=sys.stderr)
            break

        offset += 1
        time.sleep(0.3)

    return stores


def main():
    print("Fancrew スクレイプ開始", file=sys.stderr)
    stores = scrape_all()

    updated = datetime.now(timezone(timedelta(hours=9))).isoformat()
    out = {"stores": stores, "updated": updated}

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"\n完了: {len(stores)} 件 → {OUTPUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
