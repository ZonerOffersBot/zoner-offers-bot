"""Deal scoring/filtering facade kept compatible with the live engine."""
import bot

is_deal_candidate = bot.is_deal_candidate
normalize_candidate = getattr(bot, "normalize_candidate", None)

def score_deal(candidate):
    fn = getattr(bot, "score_candidate", None)
    if fn:
        return fn(candidate)
    return candidate.get("score", 0) if isinstance(candidate, dict) else 0

def fingerprint(title, url):
    return bot.fingerprint(title, url)
