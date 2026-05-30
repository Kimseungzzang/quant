import logging
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_POSITIVE_KEYWORDS = ["상승", "급등", "강세", "호재", "신고가", "매수", "상향", "실적 개선", "수주", "beat", "upgrade", "bullish", "record"]
_NEGATIVE_KEYWORDS = ["하락", "급락", "약세", "악재", "매도", "하향", "실적 부진", "소송", "리콜", "miss", "downgrade", "bearish", "recall"]

_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def get_domestic_news(stock_code: str, max_articles: int = 5) -> list[dict]:
    """네이버 금융에서 종목 뉴스 수집."""
    url = f"https://finance.naver.com/item/news_news.naver?code={stock_code}"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=5)
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "lxml")
        rows = soup.select("table.type5 tr")
        articles = []
        for row in rows:
            title_tag = row.select_one("td.title a")
            date_tag = row.select_one("td.date")
            if not title_tag:
                continue
            title = title_tag.get_text(strip=True)
            articles.append({"title": title, "date": date_tag.get_text(strip=True) if date_tag else ""})
            if len(articles) >= max_articles:
                break
        return articles
    except Exception as e:
        logger.warning("국내 뉴스 수집 실패 (%s): %s", stock_code, e)
        return []


def get_overseas_news(stock_code: str, max_articles: int = 5) -> list[dict]:
    """Yahoo Finance에서 해외주식 뉴스 수집."""
    url = f"https://finance.yahoo.com/quote/{stock_code}/news/"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=5)
        soup = BeautifulSoup(resp.text, "lxml")
        items = soup.select("h3.Mb\\(5px\\) a, li.js-stream-content h3 a")
        articles = []
        for item in items:
            title = item.get_text(strip=True)
            if title:
                articles.append({"title": title, "date": ""})
            if len(articles) >= max_articles:
                break
        return articles
    except Exception as e:
        logger.warning("해외 뉴스 수집 실패 (%s): %s", stock_code, e)
        return []


def score_sentiment(articles: list[dict]) -> float:
    """뉴스 제목 기반 감성 점수 반환 (-1.0 ~ 1.0)."""
    if not articles:
        return 0.0

    pos, neg = 0, 0
    for art in articles:
        title = art["title"].lower()
        pos += sum(1 for kw in _POSITIVE_KEYWORDS if kw.lower() in title)
        neg += sum(1 for kw in _NEGATIVE_KEYWORDS if kw.lower() in title)

    total = pos + neg
    if total == 0:
        return 0.0
    return (pos - neg) / total
