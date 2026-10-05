import asyncio
import logging
import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-IN,en;q=0.9",
}

async def fetch_page(url, timeout=15, retries=2):
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=8),
                follow_redirects=True,
                headers=HEADERS,
            ) as client:
                response = await client.get(url)
                if response.status_code == 200:
                    return response.text
                if response.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                    await asyncio.sleep(2 ** attempt)
                    continue
                logger.warning("HTTP %s for %s", response.status_code, url)
                return None
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            logger.warning("Fetch failed for %s: %s", url, exc)
            if attempt < retries:
                await asyncio.sleep(2 ** attempt)
            else:
                return None
        except Exception as exc:
            logger.exception("Unexpected fetch error for %s: %s", url, exc)
            return None
    return None

def safe_select(soup, selector, attr=None, default="N/A"):
    try:
        if not soup:
            return default
        element = soup.select_one(selector)
        if element is None:
            return default
        if attr:
            return element.get(attr, default)
        return element.get_text(" ", strip=True) or default
    except Exception:
        return default
