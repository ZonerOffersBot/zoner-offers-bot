"""Common scraper helpers; live discovery remains in bot.py."""
from urllib.parse import urlparse

def valid_url(url: str) -> bool:
    try:
        p=urlparse(url or "")
        return p.scheme in ("http","https") and bool(p.netloc)
    except Exception:
        return False
