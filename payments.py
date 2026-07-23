"""
Stripe payment helper for The PillBox Pharmacy.

Uses Stripe Checkout (Stripe-hosted payment page) so we never touch raw card
details — Stripe handles all card entry and PCI compliance. We just create a
Checkout Session and redirect the customer to it.

Currency note: the store displays prices in JMD, but Stripe (in sandbox and for
real payouts) doesn't support JMD. For this demo we charge the same numeric
amount in USD — e.g. an order totalling "JMD 2,070" is charged as $20.70 test.
No real money moves in sandbox.

Config (from environment / .env):
    STRIPE_SECRET_KEY        sk_test_...  (required to enable card payments)
    STRIPE_PUBLISHABLE_KEY   pk_test_...  (used by the frontend, optional here)
    CURRENCY                 charge currency, default "usd"
"""

import os
import stripe

CURRENCY = os.environ.get("CURRENCY", "usd").lower()


def _secret_key():
    return os.environ.get("STRIPE_SECRET_KEY", "").strip()


def is_configured():
    """True if a Stripe secret key is present so card checkout can be offered."""
    return bool(_secret_key())


def publishable_key():
    return os.environ.get("STRIPE_PUBLISHABLE_KEY", "").strip()


def create_checkout_session(order, success_url, cancel_url):
    """
    Create a Stripe Checkout Session for an order. Returns the session's URL to
    redirect the customer to, or None if Stripe isn't configured / fails.

    Each cart line becomes a Stripe line item. Shipping and tax are added as their
    own line items so the Stripe total matches the order total exactly.
    """
    if not is_configured():
        print("[stripe] not configured — cannot create checkout session")
        return None

    stripe.api_key = _secret_key()

    line_items = []
    for item in order.get("items", []):
        line_items.append({
            "price_data": {
                "currency": CURRENCY,
                "product_data": {"name": item["name"]},
                # Stripe wants the amount in the smallest unit (cents).
                "unit_amount": int(round(item["price"] * 100)),
            },
            "quantity": item["qty"],
        })

    # Shipping as a line item (if any)
    if order.get("shipping", 0) > 0:
        line_items.append({
            "price_data": {
                "currency": CURRENCY,
                "product_data": {"name": "Shipping"},
                "unit_amount": int(round(order["shipping"] * 100)),
            },
            "quantity": 1,
        })

    # Tax (GCT) as a line item so the Stripe total equals the order total
    if order.get("tax", 0) > 0:
        line_items.append({
            "price_data": {
                "currency": CURRENCY,
                "product_data": {"name": "GCT (15%)"},
                "unit_amount": int(round(order["tax"] * 100)),
            },
            "quantity": 1,
        })

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=line_items,
            customer_email=order.get("customer_email") or None,
            client_reference_id=order.get("id"),
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={"order_id": order.get("id", "")},
        )
        return session.url
    except Exception as e:
        print(f"[stripe] failed to create checkout session: {e}")
        return None


def retrieve_session(session_id):
    """Fetch a completed Checkout Session to verify payment. Returns the session or None."""
    if not is_configured() or not session_id:
        return None
    stripe.api_key = _secret_key()
    try:
        return stripe.checkout.Session.retrieve(session_id)
    except Exception as e:
        print(f"[stripe] could not retrieve session {session_id}: {e}")
        return None