"""Flipkart public product-page scraper adapter."""
from .base import fetch_page, clean_text, soup_from_html

async def scrape_flipkart(product_url):
    html = await fetch_page(product_url)
    soup = soup_from_html(html)
    if not soup:
        return None

    title = soup.select_one("span.B_NuCI") or soup.select_one("h1")
    price = soup.select_one("div._30jeq3") or soup.select_one("div.Nx9bqj")
    mrp = soup.select_one("div._3I9_wc") or soup.select_one("div.yRaY8j")

    return {
        "title": clean_text(title),
        "price": clean_text(price).replace("₹", "").replace(",", ""),
        "mrp": clean_text(mrp).replace("₹", "").replace(",", ""),
        "source": "Flipkart",
        "url": product_url,
    }

def discover():
    """Compatibility hook; live discovery is orchestrated by bot.py."""
    return []
