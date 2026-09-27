# -*- coding: utf-8 -*-
"""
서울교육뉴스 요약 텔레그램 봇 (무료 추출 요약)
- 1차 소스: seoul-news.com/app (HTML 목록 → 상세)
- 폴백 소스: 구글 뉴스 RSS ("서울교육" 검색)
- 요약: 빈도 기반 추출 요약(외부 API/키 불필요)
- 중복제거: sent.json 에 보낸 링크 기록
"""

import os
import re
import json
import html
import time
import hashlib
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

# ────────────────────────────── 설정 ──────────────────────────────
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

LIST_URL     = "http://seoul-news.com/app/sns_list"   # 목록 페이지
BASE_URL     = "http://seoul-news.com"
GOOGLE_RSS   = "https://news.google.com/rss/search?q=%EC%84%9C%EC%9A%B8%EA%B5%90%EC%9C%A1&hl=ko&gl=KR&ceid=KR:ko"

MAX_ARTICLES   = 5     # 한 번에 알림할 최대 기사 수
SUMMARY_SENTS  = 3     # 기사당 요약 문장 수
SENT_FILE      = "sent.json"
KEEP_HISTORY   = 500   # sent.json 에 보관할 최대 링크 수

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0 Safari/537.36"),
    "Accept-Language": "ko-KR,ko;q=0.9",
}
KST = timezone(timedelta(hours=9))


# ────────────────────────── 중복제거 저장소 ──────────────────────────
def load_sent():
    try:
        with open(SENT_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_sent(sent_set):
    data = list(sent_set)[-KEEP_HISTORY:]
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=0)

def key_of(url):
    return hashlib.md5(url.encode("utf-8")).hexdigest()


# ────────────────────────── 무료 추출 요약 ──────────────────────────
def split_sentences(text):
    text = re.sub(r"\s+", " ", text).strip()
    # 한국어 종결어미/문장부호 기준 분리
    parts = re.split(r"(?<=[.!?。])\s+|(?<=[다요음임])\.\s*", text)
    return [s.strip() for s in parts if len(s.strip()) > 10]

def summarize(text, n=SUMMARY_SENTS):
    sents = split_sentences(text)
    if len(sents) <= n:
        return " ".join(sents)

    # 단어 빈도 계산(2글자 이상 한글/영문/숫자)
    words = re.findall(r"[가-힣A-Za-z0-9]{2,}", text)
    freq = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1
    if not freq:
        return " ".join(sents[:n])
    mx = max(freq.values())
    for w in freq:
        freq[w] /= mx  # 정규화

    # 문장 점수 = 포함 단어 빈도 합 / 문장 길이 보정
    scored = []
    for idx, s in enumerate(sents):
        sw = re.findall(r"[가-힣A-Za-z0-9]{2,}", s)
        if not sw:
            continue
        score = sum(freq.get(w, 0) for w in sw) / (len(sw) ** 0.5)
        # 앞쪽 문장 가산점(리드 문장이 중요한 경우가 많음)
        if idx < 3:
            score *= 1.15
        scored.append((score, idx, s))

    top = sorted(scored, key=lambda x: x[0], reverse=True)[:n]
    top = sorted(top, key=lambda x: x[1])  # 원래 순서 복원
    return " ".join(s for _, _, s in top)


# ────────────────────────── 본문 추출 ──────────────────────────
def extract_body(soup):
    # 흔한 본문 컨테이너 우선 탐색
    for sel in ["article", "#article", ".article", ".view_con", ".article_body",
                ".news_body", ".content", "#content", ".cont"]:
        node = soup.select_one(sel)
        if node and len(node.get_text(strip=True)) > 200:
            return node.get_text(" ", strip=True)
    # 폴백: 가장 텍스트가 많은 div
    best, best_len = "", 0
    for div in soup.find_all(["div", "section"]):
        t = div.get_text(" ", strip=True)
        if len(t) > best_len:
            best, best_len = t, len(t)
    return best


# ────────────────────────── 1차 소스: seoul-news.com ──────────────────────────
def fetch_from_seoul_news():
    items = []
    try:
        r = requests.get(LIST_URL, headers=HEADERS, timeout=15)
        r.raise_for_status()
        r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "html.parser")

        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            title = a.get_text(strip=True)
            # 기사 상세로 보이는 링크만 필터(숫자 id / view / detail 등)
            if not title or len(title) < 8:
                continue
            if not re.search(r"(view|detail|read|idx|no|article|\d{2,})", href, re.I):
                continue
            url = urljoin(BASE_URL, href)
            if url in seen:
                continue
            seen.add(url)
            items.append({"title": title, "url": url})

        # 상세 본문 채우기
        result = []
        for it in items[: MAX_ARTICLES * 3]:
            try:
                d = requests.get(it["url"], headers=HEADERS, timeout=15)
                d.encoding = d.apparent_encoding
                dsoup = BeautifulSoup(d.text, "html.parser")
                body = extract_body(dsoup)
                if len(body) < 120:
                    continue
                it["summary"] = summarize(body)
                result.append(it)
                time.sleep(0.5)
            except Exception:
                continue
        return result
    except Exception as e:
        print("[seoul-news] 실패:", e)
        return []


# ────────────────────────── 폴백 소스: 구글 뉴스 RSS ──────────────────────────
def fetch_from_google_rss():
    items = []
    try:
        r = requests.get(GOOGLE_RSS, headers=HEADERS, timeout=15)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link  = (item.findtext("link") or "").strip()
            desc  = html.unescape(item.findtext("description") or "")
            desc  = BeautifulSoup(desc, "html.parser").get_text(" ", strip=True)
            if not title or not link:
                continue
            items.append({
                "title": title,
                "url": link,
                "summary": summarize(desc) if len(desc) > 60 else title,
            })
    except Exception as e:
        print("[google-rss] 실패:", e)
    return items


# ────────────────────────── 텔레그램 발송 ──────────────────────────
def esc(t):
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    r = requests.post(url, data=payload, timeout=15)
    if not r.ok:
        print("[telegram] 발송 실패:", r.status_code, r.text)
    r.raise_for_status()


# ────────────────────────── 메인 ──────────────────────────
def main():
    sent = load_sent()

    articles = fetch_from_seoul_news()
    source = "서울교육뉴스"
    if not articles:
        print("서울교육뉴스에서 기사를 못 가져와 구글 뉴스로 폴백합니다.")
        articles = fetch_from_google_rss()
        source = "구글 뉴스(서울교육)"

    # 중복 제거
    fresh = [a for a in articles if key_of(a["url"]) not in sent][:MAX_ARTICLES]

    if not fresh:
        print("새 기사가 없습니다. 발송 생략.")
        return

    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    header = f"📰 <b>서울교육뉴스</b> ({now})\n출처: {source}\n" + "─" * 15
    lines = [header]
    for i, a in enumerate(fresh, 1):
        lines.append(
            f"\n<b>{i}. {esc(a['title'])}</b>\n"
            f"{esc(a.get('summary', ''))}\n"
            f"🔗 <a href=\"{esc(a['url'])}\">기사 보기</a>"
        )
    send_telegram("\n".join(lines))

    for a in fresh:
        sent.add(key_of(a["url"]))
    save_sent(sent)
    print(f"{len(fresh)}건 발송 완료.")


if __name__ == "__main__":
    main()
