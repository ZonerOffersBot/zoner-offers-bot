"""Local Instamart discount calculator used by Zoner Offers Bot.

This is only a calculator. It does not connect to Swiggy, verify coupons,
or apply discounts to orders.
"""
from math import isfinite

TARGET_DISCOUNT = 300.00


def parse_rupees(raw: str) -> float:
    value = float(raw.strip().replace(",", ""))
    if not isfinite(value) or value < 0:
        raise ValueError("Amount must be a finite number that is zero or greater.")
    return round(value, 2)


def calculate(cart_total: float, minimum_order: float, maximum_discount: float,
              offer_eligible: bool) -> dict:
    errors = []
    if cart_total <= 0:
        errors.append("Cart total must be greater than ₹0.")
    if cart_total < minimum_order:
        errors.append(f"Cart must be at least ₹{minimum_order:.2f}.")
    if not offer_eligible:
        errors.append("You marked the offer as ineligible in the official app.")

    discount = 0.0
    if not errors:
        discount = min(TARGET_DISCOUNT, maximum_discount, cart_total)

    return {
        "cart_total": round(cart_total, 2),
        "minimum_order": round(minimum_order, 2),
        "requested_discount": TARGET_DISCOUNT,
        "maximum_discount": round(maximum_discount, 2),
        "calculated_discount": round(discount, 2),
        "calculated_payable": round(cart_total - discount, 2),
        "eligible_in_local_test": not errors,
        "validation_errors": errors,
    }
