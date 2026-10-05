"""Amazon public product-page scraper adapter."""
from .base import fetch_page, clean_text, soup_from_html

async def scrape_amazon(product_url):
    html = await fetch_page(product_url)
    soup = soup_from_html(html)
    if not soup:
        return None

    title = soup.select_one("#productTitle")
    price = soup.select_one(".a-price .a-offscreen") or soup.select_one(".a-price-whole")
    mrp = soup.select_one(".a-text-price .a-offscreen")
    image = soup.select_one("#landingImage")

    return {
        "title": clean_text(title),
        "price": clean_text(price).replace("₹", "").replace(",", ""),
        "mrp": clean_text(mrp).replace("₹", "").replace(",", ""),
        "image": image.get("src") if image else None,
        "source": "Amazon",
        "url": product_url,
    }

def discover():
    """Compatibility hook; live discovery is orchestrated by bot.py."""
    return []
