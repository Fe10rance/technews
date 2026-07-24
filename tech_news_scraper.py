"""
tech_news_scraper.py
----------------------
جمع‌آوری اخبار تکنولوژی از فیدهای RSS خارجی، ترجمه عنوان و خلاصه به فارسی،
استخراج تصویر شاخص، ساخت تگ، و ذخیره در data/tech_news/pending.json

این فایل توسط GitHub Actions به صورت روزانه اجرا می‌شود.
خروجی pending.json توسط پنل ادمین پیکین (از طریق jsDelivr) خوانده می‌شود
و ادمین به صورت دستی هرکدام را که خواست منتشر می‌کند.
"""

import json
import hashlib
import os
import time
import re
from datetime import datetime, timedelta, timezone

import feedparser
import requests
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

# چند روز عقب‌تر از فید را در نظر بگیریم (اخبار قدیمی‌تر نادیده گرفته می‌شوند)
MAX_ENTRY_AGE_DAYS = 3
# حداکثر تعداد آیتم در pending.json (قدیمی‌ترها پاک می‌شوند تا فایل بزرگ نشود)
MAX_PENDING_ITEMS = 150
# حداکثر تعداد guid که در حافظه seen نگه می‌داریم (برای جلوگیری از رشد بی‌نهایت فایل)
MAX_SEEN_GUIDS = 3000

REQUEST_TIMEOUT = 10
TRANSLATE_RETRY = 3
TRANSLATE_SLEEP = 1.2  # فاصله بین درخواست‌های ترجمه برای جلوگیری از بلاک شدن

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


# ==================================================
# ساخت شناسه یکتا برای هر خبر
# ==================================================

def make_guid(entry):
    base = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.md5(base.encode("utf-8")).hexdigest()


# ==================================================
# استخراج تصویر شاخص خبر
# ==================================================

def extract_image_from_entry(entry):
    # media_content (رایج در RSS با media namespace)
    media = entry.get("media_content") or entry.get("media_thumbnail")
    if media:
        for m in media:
            url = m.get("url")
            if url:
                return url

    # enclosure ها
    for link in entry.get("links", []):
        if link.get("rel") == "enclosure" and "image" in link.get("type", ""):
            return link.get("href")

    # بعضی فیدها تصویر رو داخل summary/content به صورت تگ img می‌ذارن
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
    """ فچ صفحه خبر و گرفتن og:image به عنوان راه‌حل جایگزین """
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
    # اگه از RSS چیزی گیر نیومد، سراغ og:image خود صفحه بریم
    link = entry.get("link")
    if link:
        return extract_og_image(link)
    return None


# ==================================================
# ترجمه متن با تلاش مجدد
# ==================================================

def translate_to_fa(text):
    if not text:
        return ""
    text = text.strip()
    if not text:
        return ""

    for attempt in range(TRANSLATE_RETRY):
        try:
            translated = GoogleTranslator(source="en", target="fa").translate(text)
            if translated:
                return translated
        except Exception as e:
            print(f"  ! خطای ترجمه (تلاش {attempt+1}): {e}")
            time.sleep(2)
    return text  # اگه ترجمه شکست خورد، متن اصلی رو برمی‌گردونیم تا چیزی از دست نره


# ==================================================
# پاک‌سازی خلاصه خبر از تگ‌های HTML
# ==================================================

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

    # اول از categories خود فید (اگه وجود داشته باشه)
    for t in entry.get("tags", []):
        term = t.get("term")
        if term and len(tags) < 4:
            tags.append(term)

    # اگه فید تگی نداشت، چند کلمه کلیدی از عنوان انگلیسی بگیریم
    if not tags:
        title = entry.get("title", "")
        words = re.findall(r"[A-Z][a-zA-Z]{2,}", title)
        seen = set()
        for w in words:
            if w.lower() not in seen and len(tags) < 3:
                tags.append(w)
                seen.add(w.lower())

    # ترجمه تگ‌ها به فارسی
    tags_fa = []
    for tag in tags[:4]:
        translated = translate_to_fa(tag)
        if translated:
            tags_fa.append(translated)
        time.sleep(0.5)

    # همیشه نام منبع رو هم به عنوان تگ اضافه کن
    tags_fa.append(source_name)

    # حذف تکراری‌ها با حفظ ترتیب
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

    # چک تاریخ انتشار - خبرهای خیلی قدیمی رو رد کن
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

    raw_summary = entry.get("summary", "") or entry.get("description", "")
    excerpt_en = clean_summary(raw_summary)

    print(f"  -> ترجمه: {title_en[:60]}...")
    title_fa = translate_to_fa(title_en)
    time.sleep(TRANSLATE_SLEEP)
    excerpt_fa = translate_to_fa(excerpt_en)
    time.sleep(TRANSLATE_SLEEP)

    image_url = get_best_image(entry)
    tags_fa = build_tags(entry, source_name)

    return {
        "guid": guid,
        "source": source_name,
        "source_url": entry.get("link", ""),
        "published_at": published_dt.isoformat(),
        "title_en": title_en,
        "title_fa": title_fa,
        "excerpt_en": excerpt_en,
        "excerpt_fa": excerpt_fa,
        "image_url": image_url,
        "tags_fa": tags_fa,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


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

    # ادغام با pending موجود
    combined = new_items + pending

    # حذف قدیمی‌ترها اگه از حد مجاز بیشتر شد
    combined.sort(key=lambda x: x.get("published_at", ""), reverse=True)
    combined = combined[:MAX_PENDING_ITEMS]

    save_json(PENDING_PATH, combined)

    # مدیریت اندازه seen_guids
    seen_list = list(seen_guids)
    if len(seen_list) > MAX_SEEN_GUIDS:
        seen_list = seen_list[-MAX_SEEN_GUIDS:]
    save_json(SEEN_GUIDS_PATH, seen_list)

    print(f"مجموع {len(combined)} خبر در pending.json ذخیره شد.")


if __name__ == "__main__":
    main()
