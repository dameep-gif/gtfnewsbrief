import html
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from bs4 import BeautifulSoup


KST = ZoneInfo("Asia/Seoul")
NEWS_COLUMNS = ["제목", "링크", "출처", "발행일", "수집일시"]
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.naver.com/",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}
PUBLISHED_AT_SELECTORS = [
    ("meta[property='article:published_time']", "content"),
    ("meta[property='og:article:published_time']", "content"),
    ("meta[name='article:published_time']", "content"),
    ("meta[name='publish-date']", "content"),
    ("meta[name='publish_date']", "content"),
    ("meta[name='pubdate']", "content"),
    ("meta[name='parsely-pub-date']", "content"),
    ("meta[itemprop='datePublished']", "content"),
    ("meta[property='bt:pubDate']", "content"),
    ("span[data-date-time]", "data-date-time"),
    ("time[datetime]", "datetime"),
]
DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y.%m.%d. %H:%M:%S",
    "%Y.%m.%d. %H:%M",
    "%Y.%m.%d %H:%M:%S",
    "%Y.%m.%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d",
    "%Y.%m.%d.",
    "%Y.%m.%d",
    "%Y/%m/%d",
    "%Y%m%d%H%M%S",
    "%Y%m%d",
)


def _get_requests_session():
    session = requests.Session()

    # Some local environments export a broken proxy like 127.0.0.1:9.
    # Ignore proxy env vars by default so crawling can reach news sites.
    if os.getenv("NEWS_CRAWLER_USE_SYSTEM_PROXY", "").strip().lower() not in {"1", "true", "yes", "on"}:
        session.trust_env = False

    return session


def _fetch_url(url, headers=None, timeout=10):
    request_headers = headers or DEFAULT_HEADERS
    with _get_requests_session() as session:
        response = session.get(url, headers=request_headers, timeout=timeout)
        response.encoding = "utf-8"
        return response


def _now_kst():
    return datetime.now(KST)


def _format_datetime(dt):
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    else:
        dt = dt.astimezone(KST)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _empty_news_df():
    return pd.DataFrame(columns=NEWS_COLUMNS)


def _dedupe_news_list(news_list, max_items):
    if not news_list:
        return _empty_news_df()

    df = pd.DataFrame(news_list)
    df = df.drop_duplicates(subset=["링크"], keep="first")
    return df.head(max_items)


def _normalize_link(link, base_url):
    if not link:
        return ""
    if link.startswith("http://") or link.startswith("https://"):
        return link
    if link.startswith("//"):
        return f"https:{link}"
    return urllib.parse.urljoin(base_url, link)


def _is_skip_naver_title(title):
    skip_phrases = [
        "언론사 편집",
        "주요기사",
        "구독",
        "메인에서 바로 보는",
    ]
    title_lower = title.lower()
    return any(phrase.lower() in title_lower for phrase in skip_phrases)


def _is_naver_article_link(link):
    link_lower = link.lower()
    if "news.naver.com" not in link_lower and "n.news.naver.com" not in link_lower:
        return False
    if "/main/read.naver" in link_lower or "/read.naver" in link_lower or "/v/" in link_lower:
        return True
    if "/mnews/article/" in link_lower or ("oid=" in link_lower and "aid=" in link_lower):
        return True
    return "article" in link_lower


def _is_skip_naver_link(link):
    skip_paths = [
        "main/static/channelpromotion.html",
        "/main/static/",
        "/subscriptions",
        "newsstand",
        "login.naver.com",
        "channelpromotion.html",
        "/home",
    ]
    link_lower = link.lower()
    return any(skip in link_lower for skip in skip_paths)


def _is_valid_news_link(link):
    link_lower = link.lower()
    if _is_skip_naver_link(link_lower):
        return False
    if _is_naver_article_link(link_lower):
        return True
    if "articleview.html" in link_lower or "/article/" in link_lower or "/articles/" in link_lower:
        return True
    if "/news/article" in link_lower or "/news/view" in link_lower:
        return True
    return False


def _extract_source_from_listing(node, fallback_source):
    current = node
    for _ in range(5):
        if current is None:
            break
        for selector in (
            ".sa_text_press",
            ".press",
            ".news_info .info_group .press",
            "[data-name='journalist']",
        ):
            source_node = current.select_one(selector)
            if source_node:
                source_name = source_node.get_text(" ", strip=True)
                if source_name:
                    return source_name
        current = getattr(current, "parent", None)
    return fallback_source


def _parse_datetime_string(value):
    if not value:
        return None

    text = html.unescape(str(value)).strip()
    if not text:
        return None

    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)

    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=KST)
        return parsed.astimezone(KST)
    except (TypeError, ValueError, IndexError, OverflowError):
        pass

    normalized_iso = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized_iso)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=KST)
        return parsed.astimezone(KST)
    except ValueError:
        pass

    korean_match = re.search(
        r"(20\d{2})[./-]\s*(\d{1,2})[./-]\s*(\d{1,2})\.?\s*(오전|오후)\s*(\d{1,2}):(\d{2})(?::(\d{2}))?",
        text,
    )
    if korean_match:
        year, month, day, meridiem, hour, minute, second = korean_match.groups()
        hour_value = int(hour)
        if meridiem == "오후" and hour_value < 12:
            hour_value += 12
        if meridiem == "오전" and hour_value == 12:
            hour_value = 0
        parsed = datetime(
            int(year),
            int(month),
            int(day),
            hour_value,
            int(minute),
            int(second or 0),
            tzinfo=KST,
        )
        return parsed

    for datetime_format in DATETIME_FORMATS:
        try:
            parsed = datetime.strptime(text, datetime_format).replace(tzinfo=KST)
            return parsed
        except ValueError:
            continue

    compact_match = re.search(r"\b(20\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})\b", text)
    if compact_match:
        year, month, day, hour, minute, second = compact_match.groups()
        return datetime(
            int(year),
            int(month),
            int(day),
            int(hour),
            int(minute),
            int(second),
            tzinfo=KST,
        )

    return None


def _extract_json_ld_published_at(soup):
    for script in soup.select("script[type='application/ld+json']"):
        script_text = script.string or script.get_text(" ", strip=True)
        if not script_text:
            continue
        match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', script_text)
        if match:
            parsed = _parse_datetime_string(match.group(1))
            if parsed:
                return parsed
    return None


def _extract_text_based_published_at(soup):
    for tag in soup.find_all(["time", "span", "div", "p"], limit=200):
        descriptor = " ".join(tag.get("class", []))
        descriptor = f"{descriptor} {tag.get('id', '')}".lower()
        if not any(token in descriptor for token in ("date", "time", "stamp", "publish", "pub", "기사입력")):
            continue
        parsed = _parse_datetime_string(tag.get_text(" ", strip=True))
        if parsed:
            return parsed
    return None


def _extract_published_at_from_soup(soup):
    for selector, attr_name in PUBLISHED_AT_SELECTORS:
        node = soup.select_one(selector)
        if not node:
            continue
        raw_value = node.get(attr_name, "")
        if not raw_value and attr_name == "text":
            raw_value = node.get_text(" ", strip=True)
        if not raw_value:
            raw_value = node.get_text(" ", strip=True)
        parsed = _parse_datetime_string(raw_value)
        if parsed:
            return parsed

    json_ld_value = _extract_json_ld_published_at(soup)
    if json_ld_value:
        return json_ld_value

    return _extract_text_based_published_at(soup)


@lru_cache(maxsize=512)
def _extract_article_published_at(url):
    try:
        response = _fetch_url(url, timeout=10)
        if response.status_code != 200:
            return ""

        soup = BeautifulSoup(response.content, "html.parser")
        published_at = _extract_published_at_from_soup(soup)
        return _format_datetime(published_at)
    except Exception as exc:
        print(f"발행일 확인 실패 ({url}): {exc}")
        return ""


def _build_news_record(title, link, source_name, published_at, collected_at):
    return {
        "제목": title[:100],
        "링크": link,
        "출처": source_name,
        "발행일": published_at,
        "수집일시": collected_at,
    }


def crawl_naver_news(keyword="", category="all", max_items=20):
    """
    네이버 뉴스 크롤링
    """

    category_urls = {
        "all": "https://news.naver.com/",
        "politics": "https://news.naver.com/section/100",
        "economy": "https://news.naver.com/section/101",
        "society": "https://news.naver.com/section/102",
        "life": "https://news.naver.com/section/103",
        "world": "https://news.naver.com/section/104",
    }

    urls_to_try = []
    if keyword:
        encoded_keyword = urllib.parse.quote(keyword)
        urls_to_try.append(f"https://search.naver.com/search.naver?where=news&query={encoded_keyword}")
        urls_to_try.append(f"https://news.naver.com/search/search.naver?query={encoded_keyword}&where=news")
    else:
        urls_to_try.append(category_urls.get(category, category_urls["all"]))

    collected_at = _format_datetime(_now_kst())
    news_list = []
    seen_links = set()

    try:
        for url in urls_to_try:
            try:
                response = _fetch_url(url, timeout=10)
                if response.status_code != 200:
                    continue

                soup = BeautifulSoup(response.content, "html.parser")
                articles = []

                for selector in (
                    "a.news_tit",
                    "a.sa_text_title",
                    "a[class*='news_tit']",
                    "a[class*='title']",
                ):
                    articles = soup.select(selector)
                    if len(articles) >= 3:
                        break

                if len(articles) < 3:
                    articles = [
                        anchor
                        for anchor in soup.find_all("a", href=True)
                        if len(anchor.get_text(strip=True)) > 5
                        and _is_valid_news_link(_normalize_link(anchor.get("href", ""), url))
                    ]

                if len(articles) < 3:
                    for item in soup.find_all("div", class_=lambda value: value and "title" in value.lower()):
                        link_node = item.find("a", href=True)
                        if link_node:
                            articles.append(link_node)

                for article in articles[: max_items * 4]:
                    try:
                        title = article.get("title", "") or article.get_text(strip=True)
                        link = _normalize_link(article.get("href", ""), url)
                        source_name = _extract_source_from_listing(article, "네이버 뉴스")

                        if not title or len(title.strip()) < 5:
                            continue
                        if not link or not _is_valid_news_link(link):
                            continue
                        if _is_skip_naver_title(title):
                            continue
                        if link in seen_links:
                            continue

                        seen_links.add(link)
                        published_at = _extract_article_published_at(link)
                        news_list.append(
                            _build_news_record(
                                title=title.strip(),
                                link=link,
                                source_name=source_name,
                                published_at=published_at,
                                collected_at=collected_at,
                            )
                        )

                        if len(news_list) >= max_items:
                            return _dedupe_news_list(news_list, max_items)
                    except Exception:
                        continue
            except Exception as exc:
                print(f"URL 시도 실패 ({url}): {exc}")
                continue

        return _dedupe_news_list(news_list, max_items)
    except Exception as exc:
        print(f"크롤링 오류: {exc}")
        return _empty_news_df()


def _crawl_google_news_rss(keyword, max_items):
    rss_url = (
        f"https://news.google.com/rss/search?q={urllib.parse.quote(keyword)}&hl=ko&gl=KR&ceid=KR:ko"
        if keyword
        else "https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko"
    )

    response = _fetch_url(
        rss_url,
        headers={
            **DEFAULT_HEADERS,
            "Referer": "https://news.google.com/",
        },
        timeout=10,
    )
    if response.status_code != 200:
        return _empty_news_df()

    root = ET.fromstring(response.content)
    collected_at = _format_datetime(_now_kst())
    news_list = []
    seen_links = set()

    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        source_name = (item.findtext("source") or "Google 뉴스").strip()
        published_at = _format_datetime(_parse_datetime_string(item.findtext("pubDate")))

        if source_name and title.endswith(f" - {source_name}"):
            title = title[: -(len(source_name) + 3)].strip()

        if not title or len(title) < 5 or not link or link in seen_links:
            continue

        seen_links.add(link)
        news_list.append(
            _build_news_record(
                title=title,
                link=link,
                source_name=source_name,
                published_at=published_at,
                collected_at=collected_at,
            )
        )

        if len(news_list) >= max_items:
            break

    return _dedupe_news_list(news_list, max_items)


def _crawl_google_news_html(keyword, max_items):
    encoded_keyword = urllib.parse.quote(keyword) if keyword else ""
    urls_to_try = [
        f"https://news.google.com/search?q={encoded_keyword}&hl=ko&gl=KR&ceid=KR:ko",
        f"https://www.google.com/search?q={encoded_keyword}&tbm=nws&hl=ko&gl=KR&ceid=KR:ko",
    ]

    collected_at = _format_datetime(_now_kst())
    news_list = []
    seen_links = set()
    google_headers = {
        **DEFAULT_HEADERS,
        "Referer": "https://news.google.com/",
    }

    for url in urls_to_try:
        try:
            response = _fetch_url(url, headers=google_headers, timeout=10)
            if response.status_code != 200:
                continue

            soup = BeautifulSoup(response.content, "html.parser")

            if "news.google.com" in url:
                articles = soup.select("article")
                for article in articles:
                    try:
                        link_tag = article.select_one("a.DY5T1d") or article.find("a", href=True)
                        if not link_tag:
                            continue

                        title = link_tag.get_text(strip=True)
                        link = _normalize_link(link_tag.get("href", ""), "https://news.google.com")
                        source_tag = article.select_one("div.SVJrMe span") or article.select_one("span")
                        source_name = source_tag.get_text(strip=True) if source_tag else "Google 뉴스"

                        if not title or len(title) < 5 or not link or link in seen_links:
                            continue

                        seen_links.add(link)
                        news_list.append(
                            _build_news_record(
                                title=title,
                                link=link,
                                source_name=source_name,
                                published_at="",
                                collected_at=collected_at,
                            )
                        )
                    except Exception:
                        continue
            else:
                cards = soup.select("div.dbsr") or soup.select("div.xuvV6b")
                for card in cards:
                    try:
                        anchor = card.find("a", href=True)
                        title_tag = card.select_one("div.JheGif") or card.find("div")
                        title = title_tag.get_text(strip=True) if title_tag else ""
                        link = anchor.get("href", "") if anchor else ""
                        source_tag = card.select_one("div.CEMjEf span") or card.select_one("span")
                        source_name = source_tag.get_text(strip=True) if source_tag else "Google 뉴스"

                        if link.startswith("/url?"):
                            parsed = urllib.parse.urlparse(link)
                            link = urllib.parse.parse_qs(parsed.query).get("q", [""])[0] or link

                        link = _normalize_link(link, url)
                        if not title or len(title) < 5 or not link or link in seen_links:
                            continue

                        seen_links.add(link)
                        published_at = _extract_article_published_at(link)
                        news_list.append(
                            _build_news_record(
                                title=title,
                                link=link,
                                source_name=source_name,
                                published_at=published_at,
                                collected_at=collected_at,
                            )
                        )
                    except Exception:
                        continue

            if len(news_list) >= max_items:
                break
        except Exception as exc:
            print(f"URL 시도 실패 ({url}): {exc}")
            continue

    return _dedupe_news_list(news_list, max_items)


def crawl_google_news(keyword="", max_items=20):
    """
    Google 뉴스 크롤링
    """

    try:
        rss_df = _crawl_google_news_rss(keyword, max_items)
        if not rss_df.empty:
            return rss_df
    except Exception as exc:
        print(f"Google RSS 시도 실패: {exc}")

    try:
        return _crawl_google_news_html(keyword, max_items)
    except Exception as exc:
        print(f"Google HTML 시도 실패: {exc}")
        return _empty_news_df()


def extract_summary(text, num_sentences=3):
    """
    텍스트에서 주요 문장 추출 (간단한 방식)
    """
    sentences = text.split(".")
    if len(sentences) > num_sentences:
        summary = ".".join(sentences[:num_sentences]) + "."
    else:
        summary = text
    return summary.strip()
