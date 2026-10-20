#!/usr/bin/env python3
"""
MSR (ミステリーショッピングリサーチ) 飲食モニター スクレイパー
ログイン → 都道府県×ページ を全件取得 → ジオコーディング
Usage: python scraper.py
Output: msr-data.json
"""
import base64, json, os, re, sys, time, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from patchright.sync_api import sync_playwright

LOGON_URL   = "https://www2.ms-r.com/MSR/MonitorNew/logon.asp?EntryType="
API_URL     = "https://www2.ms-r.com/MSRP/Monitor/Api/GetInvitationForSearch.php"
HEADERS     = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
LOGIN_ID    = os.environ["MSR_LOGIN_ID"]
LOGIN_PW    = os.environ["MSR_LOGIN_PW"]
GSI_GEO_URL = "https://msearch.gsi.go.jp/address-search/AddressSearch"
OUTPUT      = Path(__file__).parent.parent / "msr-data.json"
SLEEP_REQ   = 0.5
SLEEP_GEO   = 0.3
CHROME_PROFILE_DIR = Path.home() / ".cache" / "msr_chrome_profile"


def wait_otp_mail(sent_at: datetime, timeout_sec: int = 300) -> str:
    """MSRのワンタイムパスワードメールを待って6桁を返す。
    メインGmailから OTP_IMAP_USER の受信箱へ転送フィルタで届く前提（認証情報は環境変数）"""
    import email, imaplib
    from email.utils import parsedate_to_datetime
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        with imaplib.IMAP4_SSL("imap.gmail.com") as m:
            m.login(os.environ["OTP_IMAP_USER"], os.environ["OTP_IMAP_PASSWORD"])
            m.select("INBOX")
            _, ids = m.search(None, f'(FROM "noreply@ms-r.com" SINCE {sent_at:%d-%b-%Y})')
            for i in reversed(ids[0].split()):
                _, data = m.fetch(i, "(RFC822)")
                msg = email.message_from_bytes(data[0][1])
                if parsedate_to_datetime(msg["Date"]) < sent_at - timedelta(seconds=60):
                    continue
                part = next((p for p in msg.walk() if p.get_content_type() == "text/plain"), msg)
                body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                hit = re.search(r"ワンタイムパスワード[：:]\s*(\d{6})", body)
                if hit:
                    return hit.group(1)
        time.sleep(10)
    raise TimeoutError(f"MSRのワンタイムパスワードメールが{timeout_sec}秒届かない（転送フィルタを確認）")


def pass_two_step_auth(page):
    """2段階認証画面: 送信→メールのコード入力→信頼できるブラウザに登録して認証"""
    print("[login] 2段階認証を求められた", file=sys.stderr)
    sent_at = datetime.now(timezone.utc)
    page.click("#sendOTPbtn")
    page.wait_for_selector("#OneTimePassword", timeout=30000)
    code = wait_otp_mail(sent_at)
    page.fill("#OneTimePassword", code)
    page.check("#saveBrowser")
    page.click("#confirmOTPbtn")


def login() -> requests.Session:
    with sync_playwright() as p:
        # headless=Trueだとms-r.com側のCloudflareボット判定を突破できないため、
        # headful(要Xvfb。msr_run.shでxvfb-run経由起動)で実行する
        ctx = p.chromium.launch_persistent_context(
            str(CHROME_PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 800},
            args=["--no-sandbox"],
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
        )
        page = ctx.new_page()
        page.goto(LOGON_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)  # Cloudflare JS challenge 完了待ち
        print(f"[login] URL={page.url} title={page.title()}", file=sys.stderr)
        print(f"[login] HTML[:500]={page.content()[:500]}", file=sys.stderr)
        page.wait_for_selector('input[name="LogonID"]', timeout=30000)
        page.fill('input[name="LogonID"]', LOGIN_ID)
        page.fill('#Password', LOGIN_PW)
        page.get_by_role("button", name="モニターサイトへ ログイン").click()
        page.wait_for_url(re.compile(r"/MSRP/Monitor/|TwoStepAuth"), timeout=30000)
        if "TwoStepAuth" in page.url:
            pass_two_step_auth(page)
        page.wait_for_url("**/MSRP/Monitor/**", timeout=30000)
        cookies = ctx.cookies()
        ctx.close()
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
        inv_decoded = base64.b64decode(s["inv_id"]).decode() if s["inv_id"] else ""
        url = f"https://www2.ms-r.com/MSR/MonitorNew/MyPage/EntryInput.asp?InvitationID={inv_decoded}" if inv_decoded else "https://www2.ms-r.com/MSRP/Monitor/Search/Search.php"
        stores_out.append({
            "name": s["name"], "address": s["address"],
            "lat": lat, "lng": lng,
            "reward": s["reward"], "budget": "",
            "url": url,
        })
        if (i + 1) % 100 == 0:
            print(f"  geocode {i+1}/{len(unique)}", file=sys.stderr)
        time.sleep(SLEEP_GEO)

    out = {"stores": stores_out, "updated": datetime.now(timezone(timedelta(hours=9))).isoformat()}
    OUTPUT.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"\n完了: {len(stores_out)}件 → {OUTPUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
