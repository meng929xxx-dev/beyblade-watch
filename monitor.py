# -*- coding: utf-8 -*-
"""Funbox BEYBLADE X 新品監控（GitHub Actions 免費雲端版）

流程：
1. 用無頭瀏覽器開啟 Funbox 官方商城「戰鬥陀螺」分類頁（含分頁）
2. 收集所有商品網址與名稱
3. 與 known_products.json（上次清單）比對
4. 有新品 → 透過 LINE 與 Email 通知
5. 更新 known_products.json（由 workflow 自動 commit 回倉庫）
"""
import json
import os
import smtplib
import ssl
import sys
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

BASE = "https://shop.funbox.com.tw"
CATEGORY_URL = f"{BASE}/categories/takaratomy/beyblade"
STATE_FILE = Path("known_products.json")
MAX_PAGES = 10

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


def scrape_products() -> dict:
    """回傳 {商品網址: 商品名稱}"""
    products: dict[str, str] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(user_agent=UA, locale="zh-TW")
        page = ctx.new_page()
        for n in range(1, MAX_PAGES + 1):
            page.goto(f"{CATEGORY_URL}?page={n}",
                      wait_until="domcontentloaded", timeout=60_000)
            try:
                page.wait_for_selector("a[href*='/products/']", timeout=20_000)
            except Exception:
                break  # 此頁沒有商品 → 結束
            page.wait_for_timeout(2_000)  # 等動態內容載完
            links = page.eval_on_selector_all(
                "a[href*='/products/']",
                "els => els.map(e => ({href: e.href, "
                "text: (e.innerText || '').replace(/\\s+/g, ' ').trim()}))",
            )
            page_items: dict[str, str] = {}
            for item in links:
                href = item["href"].split("?")[0].rstrip("/")
                if "/products/" not in href:
                    continue
                name = item["text"]
                if href not in page_items or len(name) > len(page_items[href]):
                    page_items[href] = name
            new_keys = set(page_items) - set(products)
            for k, v in page_items.items():
                if k not in products or len(v) > len(products[k]):
                    products[k] = v
            if not new_keys:
                break  # 頁面內容重複 → 已到最後一頁
        browser.close()
    return products


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


def main() -> None:
    products = scrape_products()
    print(f"本次抓到 {len(products)} 項商品")
    if not products:
        print("⚠️ 沒抓到任何商品（網站可能暫時擋爬或改版），本次不更動清單。")
        return

    if not STATE_FILE.exists():
        STATE_FILE.write_text(
            json.dumps(products, ensure_ascii=False, indent=2), "utf-8")
        msg = (f"✅ 戰鬥陀螺監控已啟動！\n已建立基準清單，"
               f"目前 Funbox 官方商城共 {len(products)} 項商品。\n"
               f"之後有新上架會立刻通知你。")
        notify_line(msg)
        notify_email("✅ 戰鬥陀螺監控已啟動", msg)
        return

    known = json.loads(STATE_FILE.read_text("utf-8"))
    new_items = {u: n for u, n in products.items() if u not in known}

    # 合併後寫回（保留舊紀錄，避免暫時抓不到的商品被誤判為之後的新品）
    known.update(products)
    STATE_FILE.write_text(
        json.dumps(known, ensure_ascii=False, indent=2), "utf-8")

    if not new_items:
        print("無新品。")
        return

    lines = [f"🆕 Funbox 上架 {len(new_items)} 件戰鬥陀螺新品！", ""]
    for url, name in new_items.items():
        lines.append(f"▶ {name or '（名稱待確認）'}")
        lines.append(url)
        lines.append("")
    msg = "\n".join(lines).strip()
    print(msg)
    notify_line(msg)
    notify_email(f"🆕 戰鬥陀螺新品 x{len(new_items)}（Funbox 官方）", msg)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # 失敗不要讓整個 workflow 紅燈轟炸信箱
        print(f"執行錯誤：{e}", file=sys.stderr)
