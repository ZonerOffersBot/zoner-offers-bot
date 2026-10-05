"""Common async HTTP/HTML scraper helpers."""
import httpx
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36",
    "Accept-Language": "en-IN,en;q=0.9",
}

async def fetch_page(url):
    """Fetch a public page asynchronously; return HTML or None."""
    try:
        timeout = httpx.Timeout(15.0, connect=8.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=HEADERS) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text
    except Exception as exc:
        print(f"Fetch error for {url}: {exc}")
        return None

def clean_text(node):
    return node.get_text(" ", strip=True) if node else "N/A"

def soup_from_html(html):
    return BeautifulSoup(html, "html.parser") if html else None
