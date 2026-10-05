"""Flipkart scraper adapter."""
import bot

def discover():
    fn=getattr(bot, "discover_candidates", None)
    return fn() if fn else []
