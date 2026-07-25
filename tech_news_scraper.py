"""
tech_news_scraper.py  (نسخه ۲)
----------------------
جمع‌آوری اخبار تکنولوژی از فیدهای RSS خارجی، ترجمه عنوان/خلاصه/متن کامل به فارسی،
استخراج تصویر شاخص، ساخت تگ، و ذخیره در data/tech_news/pending.json

تغییرات نسبت به نسخه قبل:
  - متن کامل خبر (نه فقط خلاصه) استخراج و ترجمه می‌شود (با trafilatura)
  - خبرهای قدیمی‌تر از PRUNE_AFTER_DAYS به صورت خودکار از pending.json حذف می‌شوند
"""

import json
import hashlib
import os
import time
import re
from datetime import datetime, timedelta, timezone

import feedparser
import requests
import trafilatura
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator

# ==================================================
# تنظیمات
# ==================================================

FEEDS = {
    "TechCrunch": "https://techcrunch.com/feed/",
    "The Verge": "https://www.theverge.com/rss/index.xml",
    "Engadget": "https://www.engadget.com/rss.xml",
}

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tech_news")
PENDING_PATH = os.path.join(DATA_DIR, "pending.json")
SEEN_GUIDS_PATH = os.path.join(DATA_DIR, "seen_guids.json")

# چند روز عقب‌تر از فید را در نظر بگیریم (اخبار قدیمی‌تر از این نادیده گرفته می‌شوند)
MAX_ENTRY_AGE_DAYS = 3

# خبرهایی که در pending.json قدیمی‌تر از این تعداد روز شدند حذف می‌شوند
# (چه بررسی شده باشند چه نه - فقط برای تمیز نگه داشتن فایل روی گیت‌هاب)
PRUNE_AFTER_DAYS = 7

# حداکثر تعداد آیتم در pending.json (سقف امنیتی اضافه بر پاک‌سازی زمانی)
MAX_PENDING_ITEMS = 150

# حداکثر تعداد guid که در حافظه seen نگه می‌داریم
MAX_SEEN_GUIDS = 3000

# سقف طول متن کامل قبل از ترجمه (برای جلوگیری از کندی/بلاک شدن سرویس ترجمه رایگان)
MAX_CONTENT_CHARS = 6000
TRANSLATE_CHUNK_SIZE = 4000  # گوگل ترنسلیت رایگان محدودیت کاراکتری داره

REQUEST_TIMEOUT = 12
TRANSLATE_RETRY = 3
TRANSLATE_SLEEP = 1.2

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


# ==================================================
# توابع کمکی: خواندن/نوشتن فایل‌های JSON
# ==================================================

def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def make_guid(entry):
    base = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.md5(base.encode("utf-8")).hexdigest()


# ==================================================
# استخراج تصویر شاخص خبر
# ==================================================

def extract_image_from_entry(entry):
    media = entry.get("media_content") or entry.get("media_thumbnail")
    if media:
        for m in media:
            url = m.get("url")
            if url:
                return url

    for link in entry.get("links", []):
        if link.get("rel") == "enclosure" and "image" in link.get("type", ""):
            return link.get("href")

    html_blob = ""
    if entry.get("summary"):
        html_blob += entry["summary"]
    if entry.get("content"):
        for c in entry["content"]:
            html_blob += c.get("value", "")

    if html_blob:
        soup = BeautifulSoup(html_blob, "html.parser")
        img = soup.find("img")
        if img and img.get("src"):
            return img["src"]

    return None


def extract_og_image(url):
    try:
        res = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if res.status_code != 200:
            return None
        soup = BeautifulSoup(res.text, "html.parser")
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            return og["content"]
    except requests.RequestException:
        return None
    return None


def get_best_image(entry):
    img = extract_image_from_entry(entry)
    if img:
        return img
    link = entry.get("link")
    if link:
        return extract_og_image(link)
    return None


# ==================================================
# استخراج متن کامل خبر از صفحه اصلی
# ==================================================

def extract_full_content(url):
    """
    با trafilatura متن اصلی مقاله رو (بدون منو/تبلیغ/کامنت) از صفحه استخراج می‌کنه.
    اگه شکست بخوره، None برمی‌گردونه و کد از summary فید به عنوان جایگزین استفاده می‌کنه.
    """
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
        if not text:
            return None
        text = text.strip()
        if len(text) > MAX_CONTENT_CHARS:
            text = text[:MAX_CONTENT_CHARS].rsplit(" ", 1)[0] + "..."
        return text
    except Exception as e:
        print(f"  ! خطا در استخراج متن کامل: {e}")
        return None


# ==================================================
# ترجمه متن (با پشتیبانی از متن‌های طولانی)
# ==================================================

def translate_chunk(text):
    if not text or not text.strip():
        return ""
    for attempt in range(TRANSLATE_RETRY):
        try:
            translated = GoogleTranslator(source="en", target="fa").translate(text)
            if translated:
                return translated
        except Exception as e:
            print(f"  ! خطای ترجمه (تلاش {attempt+1}): {e}")
            time.sleep(2)
    return text


def translate_to_fa(text):
    """ ترجمه متن؛ اگه طولانی باشه، تکه‌تکه ترجمه و دوباره به هم وصل می‌شه """
    if not text:
        return ""
    text = text.strip()
    if not text:
        return ""

    if len(text) <= TRANSLATE_CHUNK_SIZE:
        return translate_chunk(text)

    paragraphs = text.split("\n")
    chunks = []
    current = ""
    for p in paragraphs:
        if len(current) + len(p) + 1 > TRANSLATE_CHUNK_SIZE:
            if current:
                chunks.append(current)
            current = p
        else:
            current = (current + "\n" + p) if current else p
    if current:
        chunks.append(current)

    translated_parts = []
    for chunk in chunks:
        translated_parts.append(translate_chunk(chunk))
        time.sleep(TRANSLATE_SLEEP)

    return "\n".join(translated_parts)


def clean_summary(raw_summary, max_len=280):
    if not raw_summary:
        return ""
    text = BeautifulSoup(raw_summary, "html.parser").get_text(separator=" ").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0] + "..."
    return text


# ==================================================
# ساخت تگ برای خبر
# ==================================================

def build_tags(entry, source_name):
    tags = []
    for t in entry.get("tags", []):
        term = t.get("term")
        if term and len(tags) < 4:
            tags.append(term)

    if not tags:
        title = entry.get("title", "")
        words = re.findall(r"[A-Z][a-zA-Z]{2,}", title)
        seen = set()
        for w in words:
            if w.lower() not in seen and len(tags) < 3:
                tags.append(w)
                seen.add(w.lower())

    tags_fa = []
    for tag in tags[:4]:
        translated = translate_chunk(tag)
        if translated:
            tags_fa.append(translated)
        time.sleep(0.5)

    tags_fa.append(source_name)

    final_tags = []
    for t in tags_fa:
        if t not in final_tags:
            final_tags.append(t)
    return final_tags


# ==================================================
# پردازش یک آیتم فید
# ==================================================

def process_entry(entry, source_name, seen_guids):
    guid = make_guid(entry)
    if guid in seen_guids:
        return None

    published_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if published_struct:
        published_dt = datetime(*published_struct[:6], tzinfo=timezone.utc)
    else:
        published_dt = datetime.now(timezone.utc)

    if published_dt < datetime.now(timezone.utc) - timedelta(days=MAX_ENTRY_AGE_DAYS):
        return None

    title_en = entry.get("title", "").strip()
    if not title_en:
        return None

    link = entry.get("link", "")
    raw_summary = entry.get("summary", "") or entry.get("description", "")
    excerpt_en = clean_summary(raw_summary)

    print(f"  -> پردازش: {title_en[:60]}...")

    full_content_en = extract_full_content(link) if link else None
    if not full_content_en:
        full_content_en = excerpt_en or title_en

    title_fa = translate_to_fa(title_en)
    time.sleep(TRANSLATE_SLEEP)
    excerpt_fa = translate_to_fa(excerpt_en)
    time.sleep(TRANSLATE_SLEEP)
    content_fa = translate_to_fa(full_content_en)
    time.sleep(TRANSLATE_SLEEP)

    image_url = get_best_image(entry)
    tags_fa = build_tags(entry, source_name)

    return {
        "guid": guid,
        "source": source_name,
        "source_url": link,
        "published_at": published_dt.isoformat(),
        "title_en": title_en,
        "title_fa": title_fa,
        "excerpt_en": excerpt_en,
        "excerpt_fa": excerpt_fa,
        "content_en": full_content_en,
        "content_fa": content_fa,
        "image_url": image_url,
        "tags_fa": tags_fa,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# ==================================================
# پاک‌سازی زمانی pending.json
# ==================================================

def prune_old_items(items):
    cutoff = datetime.now(timezone.utc) - timedelta(days=PRUNE_AFTER_DAYS)
    kept = []
    removed = 0
    for item in items:
        pub = item.get("published_at") or item.get("fetched_at")
        try:
            pub_dt = datetime.fromisoformat(pub)
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pub_dt = datetime.now(timezone.utc)

        if pub_dt >= cutoff:
            kept.append(item)
        else:
            removed += 1

    if removed:
        print(f"{removed} خبر قدیمی‌تر از {PRUNE_AFTER_DAYS} روز از pending.json حذف شد.")
    return kept


# ==================================================
# اجرای اصلی
# ==================================================

def main():
    seen_guids = set(load_json(SEEN_GUIDS_PATH, []))
    pending = load_json(PENDING_PATH, [])
    existing_guids = {item["guid"] for item in pending}

    new_items = []

    for source_name, feed_url in FEEDS.items():
        print(f"در حال دریافت فید: {source_name}")
        try:
            feed = feedparser.parse(feed_url)
        except Exception as e:
            print(f"  ! خطا در دریافت فید {source_name}: {e}")
            continue

        for entry in feed.entries:
            guid = make_guid(entry)
            if guid in seen_guids or guid in existing_guids:
                continue
            try:
                item = process_entry(entry, source_name, seen_guids)
            except Exception as e:
                print(f"  ! خطا در پردازش خبر: {e}")
                continue
            if item:
                new_items.append(item)
                seen_guids.add(item["guid"])

    print(f"\n{len(new_items)} خبر جدید پیدا شد.")

    combined = new_items + pending

    combined = prune_old_items(combined)
    combined.sort(key=lambda x: x.get("published_at", ""), reverse=True)
    combined = combined[:MAX_PENDING_ITEMS]

    save_json(PENDING_PATH, combined)

    seen_list = list(seen_guids)
    if len(seen_list) > MAX_SEEN_GUIDS:
        seen_list = seen_list[-MAX_SEEN_GUIDS:]
    save_json(SEEN_GUIDS_PATH, seen_list)

    print(f"مجموع {len(combined)} خبر در pending.json ذخیره شد.")


if __name__ == "__main__":
    main()
