# -*- coding: utf-8 -*-
"""
서울교육뉴스 요약 텔레그램 봇 (무료 추출 요약 + 날짜 지정)
- 평소: 오늘 발행된 기사만 발송
- 놓친 날짜: --date / --from~--to / --days-back 으로 지정 조회
- 중복제거: sent.json
"""

import os
import re
import json
import html
import time
import argparse
import hashlib
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, quote
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

# ────────────────────────── 설정 ──────────────────────────
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

SENT_FILE     = "sent.json"
KEEP_HISTORY  = 800
SUMMARY_SENTS = 3
BATCH_SIZE    = 20     # 한 번에 보낼 최대 기사 수(도배/rate limit 방지)

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0 Safari/537.36"),
    "Accept-Language": "ko-KR,ko;q=0.9",
}
KST = timezone(timedelta(hours=9))


# ────────────────────────── 실행 인자(날짜) ──────────────────────────
def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--date")                       # 특정 하루: 2025-09-22
    p.add_argument("--from", dest="date_from")     # 기간 시작
    p.add_argument("--to",   dest="date_to")       # 기간 끝
    p.add_argument("--days-back", type=int)        # 오늘 포함 최근 N일
    return p.parse_args()

def resolve_range(args):
    """(start_date, end_date) date 객체, 양끝 포함. 인자 없으면 오늘~오늘."""
    today = datetime.now(KST).date()
    if args.date:
        d = datetime.strptime(args.date, "%Y-%m-%d").date()
        return d, d
    if args.date_from or args.date_to:
        s = datetime.strptime(args.date_from, "%Y-%m-%d").date() if args.date_from else today
        e = datetime.strptime(args.date_to,   "%Y-%m-%d").date() if args.date_to   else today
        return s, e
    if args.days_back:
        return today - timedelta(days=args.days_back - 1), today
    return today, today


# ────────────────────────── 중복제거 저장소 ──────────────────────────
def load_sent():
    try:
        with open(SENT_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_sent(sent_set):
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(list(sent_set)[-KEEP_HISTORY:], f, ensure_ascii=False, indent=0)

def key_of(url):
    return hashlib.md5(url.encode("utf-8")).hexdigest()


# ────────────────────────── 무료 추출 요약 ──────────────────────────
def split_sentences(text):
    text = re.sub(r"\s+", " ", text).strip()
    parts = re.split(r"(?<=[.!?。])\s+|(?<=[다요음임])\.\s*", text)
    return [s.strip() for s in parts if len(s.strip()) > 10]

def summarize(text, n=SUMMARY_SENTS):
    sents = split_sentences(text)
    if len(sents) <= n:
        return " ".join(sents)
    words = re.findall(r"[가-힣A-Za-z0-9]{2,}", text)
    freq = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1
    if not freq:
        return " ".join(sents[:n])
    mx = max(freq.values())
    for w in freq:
        freq[w] /= mx
    scored = []
    for idx, s in enumerate(sents):
        sw = re.findall(r"[가-힣A-Za-z0-9]{2,}", s)
        if not sw:
            continue
        score = sum(freq.get(w, 0) for w in sw) / (len(sw) ** 0.5)
        if idx < 3:
            score *= 1.15
        scored.append((score, idx, s))
    top = sorted(scored, key=lambda x: x[0], reverse=True)[:n]
    top = sorted(top, key=lambda x: x[1])
    return " ".join(s for _, _, s in top)


# ────────────────────────── 소스: 구글 뉴스 RSS (날짜 필터) ──────────────────────────
def fetch_google_rss(start, end):
    """start, end: date 객체(양끝 포함). pubDate로 정확히 필터."""
    q = (f"서울교육 after:{start - timedelta(days=1)} "
         f"before:{end + timedelta(days=1)}")
    rss = f"https://news.google.com/rss/search?q={quote(q)}&hl=ko&gl=KR&ceid=KR:ko"

    items = []
    try:
        r = requests.get(rss, headers=HEADERS, timeout=15)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link") or "").strip()
            pub   = item.findtext("pubDate")
            if not title or not link or not pub:
                continue
            try:
                pdate = parsedate_to_datetime(pub).astimezone(KST).date()
            except Exception:
                continue
            if not (start <= pdate <= end):       # ★ 날짜 범위 밖이면 제외
                continue
            desc = html.unescape(item.findtext("description") or "")
            desc = BeautifulSoup(desc, "html.parser").get_text(" ", strip=True)
            items.append({
                "title": title, "url": link, "date": pdate.isoformat(),
                "summary": summarize(desc) if len(desc) > 60 else title,
            })
    except Exception as e:
        print("[google-rss] 실패:", e)
    return items


# ────────────────────────── 텔레그램 발송 ──────────────────────────
def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": False}
    r = requests.post(url, data=payload, timeout=15)
    if not r.ok:
        print("[telegram] 발송 실패:", r.status_code, r.text)
    r.raise_for_status()


# ────────────────────────── 메인 ──────────────────────────
def main():
    args = parse_args()
    start, end = resolve_range(args)
    print(f"조회 범위: {start} ~ {end}")

    sent = load_sent()
    articles = fetch_google_rss(start, end)
    articles.sort(key=lambda a: a.get("date", ""))     # 과거→최신 순

    fresh = [a for a in articles if key_of(a["url"]) not in sent]
    if not fresh:
        print("해당 기간에 새(안 보낸) 기사가 없습니다. 발송 생략.")
        return

    rng = f"{start}" if start == end else f"{start} ~ {end}"
    for i0 in range(0, len(fresh), BATCH_SIZE):
        batch = fresh[i0:i0 + BATCH_SIZE]
        lines = [f"📰 <b>서울교육뉴스</b> ({rng})\n" + "─" * 15]
        for i, a in enumerate(batch, i0 + 1):
            lines.append(
                f"\n<b>{i}. {esc(a['title'])}</b>  <i>{a.get('date','')}</i>\n"
                f"{esc(a.get('summary',''))}\n"
                f"🔗 <a href=\"{esc(a['url'])}\">기사 보기</a>"
            )
        send_telegram("\n".join(lines))
        time.sleep(1.5)

    for a in fresh:
        sent.add(key_of(a["url"]))
    save_sent(sent)
    print(f"{len(fresh)}건 발송 완료.")


if __name__ == "__main__":
    main()
