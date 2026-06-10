# -*- coding: utf-8 -*-
"""Funbox BEYBLADE X 新品＋補貨監控（GitHub Actions 雲端長跑版）

運作方式（配合官網「5 的倍數分鐘上架」的規律）：
- workflow 每 30 分鐘啟動一個 job，每個 job 連續跑 LOOP_MINUTES 分鐘（接力覆蓋全天）
- job 內平時待機，每逢 5 分整點（XX:00、XX:05…）前 20 秒進入衝刺：
  每 10 秒掃一次類別頁，持續到整點後 100 秒
- 偵測兩種事件，LINE＋Email 通知（不自動下單）：
  1. 新品：沒見過的商品網址出現
  2. 補貨：見過的商品重新出現在架上（Funbox 賣完會下架）
- 名稱含「預購」或「售完」的不通知
"""
import json
import os
import smtplib
import ssl
import sys
import time as time_mod
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

BASE = "https://shop.funbox.com.tw"
CATEGORY_URL = f"{BASE}/categories/takaratomy/beyblade"
KNOWN_FILE = Path("known_products.json")    # 看過的所有商品 {url: name}
LISTED_FILE = Path("listed_state.json")     # 上一輪「正在架上」的網址清單
MAX_PAGES = 10

LOOP_MINUTES = int(os.environ.get("LOOP_MINUTES", "0"))  # 0 = 只掃一輪（手動測試）
TEST_MODE = os.environ.get("TEST_MODE") == "1"  # 測試：預購當新品、單輪、不存狀態
BURST_BEFORE = 20    # 5 分整點前幾秒開始衝刺
BURST_AFTER = 100    # 整點後再持續幾秒
BURST_EVERY = 10     # 衝刺期間每幾秒掃一次

SOLDOUT_HINTS = ["售完", "補貨中", "已售完", "sold out"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def scrape_products(page):
    """回傳 (可購商品dict, 原始連結數)。排除預購與標示售完者。"""
    products: dict[str, str] = {}
    raw_count = 0
    for n in range(1, MAX_PAGES + 1):
        page.goto(f"{CATEGORY_URL}?page={n}",
                  wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_selector("a[href*='/products/']", timeout=15_000)
        except Exception:
            break
        page.wait_for_timeout(1_200)
        links = page.eval_on_selector_all(
            "a[href*='/products/']",
            "els => els.map(e => ({href: e.href, "
            "text: (e.innerText || '').replace(/\\s+/g, ' ').trim()}))",
        )
        raw_count += len(links)
        page_items: dict[str, str] = {}
        for item in links:
            href = item["href"].split("?")[0].rstrip("/")
            if "/products/" not in href:
                continue
            name = item["text"]
            if href not in page_items or len(name) > len(page_items[href]):
                page_items[href] = name
        # 先彙整出每個商品的完整名稱，再過濾（商品卡的圖片連結沒文字，先過濾會漏）
        page_items = {h: n for h, n in page_items.items()
                      if (TEST_MODE or "預購" not in n)
                      and not any(s in n.lower() for s in SOLDOUT_HINTS)}
        new_keys = set(page_items) - set(products)
        for k, v in page_items.items():
            if k not in products or len(v) > len(products[k]):
                products[k] = v
        if not new_keys:
            break
    return products, raw_count


def notify_line(text: str) -> None:
    token = os.environ.get("LINE_CHANNEL_TOKEN", "").strip()
    user_id = os.environ.get("LINE_USER_ID", "").strip()
    if not token or not user_id:
        print("（未設定 LINE 金鑰，略過 LINE 通知）")
        return
    r = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {token}"},
        json={"to": user_id,
              "messages": [{"type": "text", "text": text[:4900]}]},
        timeout=30,
    )
    print(f"LINE 通知結果：{r.status_code} {r.text[:200]}")


def notify_email(subject: str, body: str) -> None:
    sender = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    to_addr = os.environ.get("NOTIFY_EMAIL", sender).strip()
    if not sender or not password:
        print("（未設定 Gmail 金鑰，略過 Email 通知）")
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = sender
    msg["To"] = to_addr
    with smtplib.SMTP_SSL("smtp.gmail.com", 465,
                          context=ssl.create_default_context()) as s:
        s.login(sender, password)
        s.sendmail(sender, [to_addr], msg.as_string())
    print(f"Email 已寄出至 {to_addr}")


def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def check_once(page, known: dict, listed_prev: set, baseline: bool):
    """掃一輪；回傳 (新的 listed set, 是否成功)。發現新品/補貨即通知。"""
    products, raw = scrape_products(page)
    if raw == 0:
        log("⚠️ 連結數 0，疑似被擋或網站異常，跳過本輪")
        return listed_prev, False
    listed_now = set(products)
    if baseline:
        log(f"建立基準：架上 {len(listed_now)} 件可購商品")
    else:
        appeared = listed_now - listed_prev
        if appeared:
            lines = []
            for url in sorted(appeared):
                name = products[url]
                kind = "補貨" if url in known else "新品"
                log(f"🆕 {kind}：{name} {url}")
                lines.append(f"🆕【{kind}】{name}")
                lines.append(url)
                lines.append("")
            msg = "\n".join(lines).strip()
            if TEST_MODE:
                msg = "🧪 測試通知（驗證鏈路用，非真實上架）\n" + msg
            notify_line(msg)
            notify_email(f"🆕 戰鬥陀螺 上架通知 x{len(appeared)}（Funbox）", msg)
        else:
            log(f"無變化（架上 {len(listed_now)} 件）")
    if not TEST_MODE:
        known.update(products)
        save_json(KNOWN_FILE, known)
        save_json(LISTED_FILE, sorted(listed_now))
    return listed_now, True


def seconds_to_next_mark() -> float:
    """距離下一個 5 分整點的秒數"""
    return 300 - (time_mod.time() % 300)


def main() -> None:
    known: dict = load_json(KNOWN_FILE, {})
    listed_raw = load_json(LISTED_FILE, None)
    baseline = listed_raw is None
    listed_prev: set = set(listed_raw or [])
    if TEST_MODE:
        # 測試模式：當作架上是空的 → 掃到的東西全部視為「上架事件」觸發通知
        log("🧪 測試模式：預購視為新品、單輪掃描、不寫入狀態")
        baseline = False
        listed_prev = set()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(user_agent=UA, locale="zh-TW")
        page = ctx.new_page()

        # 啟動先掃一輪（建立基準或接上前一個 job 的進度）
        listed_prev, _ = check_once(page, known, listed_prev, baseline)
        baseline = False

        if LOOP_MINUTES <= 0:
            browser.close()
            return

        deadline = time_mod.time() + LOOP_MINUTES * 60
        log(f"長跑模式 {LOOP_MINUTES} 分鐘：5 分整點前 {BURST_BEFORE}s 起每 "
            f"{BURST_EVERY}s 衝刺，至整點後 {BURST_AFTER}s")
        while time_mod.time() < deadline:
            wait = seconds_to_next_mark() - BURST_BEFORE
            if wait > 0:
                time_mod.sleep(min(wait, max(deadline - time_mod.time(), 0)))
            if time_mod.time() >= deadline:
                break
            burst_end = min(time_mod.time() + BURST_BEFORE + BURST_AFTER, deadline)
            log("⚡ 進入衝刺時段")
            while time_mod.time() < burst_end:
                t0 = time_mod.time()
                try:
                    listed_prev, _ = check_once(page, known, listed_prev, False)
                except Exception as e:
                    log(f"掃描錯誤：{e}")
                elapsed = time_mod.time() - t0
                if (rest := BURST_EVERY - elapsed) > 0:
                    time_mod.sleep(min(rest, max(burst_end - time_mod.time(), 0)))
        browser.close()
        log("本輪 job 結束，等待下一棒接力")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # 失敗不要讓整個 workflow 紅燈轟炸信箱
        print(f"執行錯誤：{e}", file=sys.stderr)
