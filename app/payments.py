"""Razorpay test-mode payment links + WhatsApp deal summary, both with zero-config fallbacks."""

from __future__ import annotations

import logging
import re
import time
from urllib.parse import quote

import httpx

from app import config
from app.pricing import format_inr, round_to

log = logging.getLogger("payments")


def token_advance(price: int) -> int:
    return max(100, round_to(price * config.settings.token_advance_percent / 100, 50))


async def create_payment_link(session_id: str, leg_id: str, vendor_name: str, price: int, summary: str) -> dict:
    s = config.settings
    amount = token_advance(price)
    description = f"Token advance for {vendor_name}: {summary}"[:240]
    if s.razorpay_key_id and s.razorpay_key_secret:
        body = {
            "amount": amount * 100, "currency": "INR", "accept_partial": False,
            "description": description, "reference_id": f"swarm-{session_id}-{leg_id}-{int(time.time())}"[:40],
            "notify": {"sms": False, "email": False}, "reminder_enable": False,
            "notes": {"session_id": session_id, "vendor": vendor_name, "deal_price": str(price)},
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post("https://api.razorpay.com/v1/payment_links", json=body,
                                      auth=(s.razorpay_key_id, s.razorpay_key_secret))
            if r.status_code < 400:
                data = r.json()
                return {"provider": "razorpay", "id": data.get("id"), "url": data.get("short_url"),
                        "amount": amount, "deal_price": price, "status": data.get("status", "created"),
                        "test_mode": s.razorpay_key_id.startswith("rzp_test")}
            log.warning("razorpay %s: %s", r.status_code, r.text[:300])
            error = f"Razorpay {r.status_code}"
        except httpx.HTTPError as exc:
            error = f"Razorpay unreachable: {exc}"
    else:
        error = ""
    return {"provider": "mock", "id": f"plink_mock_{session_id}", "url": f"#checkout/{session_id}",
            "amount": amount, "deal_price": price, "status": "created", "test_mode": True, "error": error}


def deal_summary(request: dict, vendor_name: str, price: int, payment: dict, savings: int | None) -> str:
    lines = [
        "✅ Deal locked by Swarm Negotiator",
        f"Job: {request.get('summary') or request.get('category')}",
        f"When: {request.get('when') or '-'}",
        f"Vendor: {vendor_name}",
        f"All-inclusive price: {format_inr(price)}",
        f"UPI token advance: {format_inr(payment['amount'])}",
    ]
    if payment.get("provider") == "razorpay" and payment.get("url"):
        lines.append(f"Pay: {payment['url']}")
    if savings:
        lines.append(f"Saved vs. highest first quote: {format_inr(savings)}")
    lines.append("Your number was never shared with any vendor.")
    return "\n".join(lines)


async def whatsapp_summary(text: str) -> dict:
    """Always returns a click-to-chat link; also sends via WhatsApp Cloud API when configured."""
    s = config.settings
    to = re.sub(r"\D", "", s.buyer_whatsapp or "")
    link = f"https://wa.me/{to}?text={quote(text)}" if to else f"https://wa.me/?text={quote(text)}"
    result = {"link": link, "sent": False, "text": text}
    if s.whatsapp_token and s.whatsapp_phone_number_id and to:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    f"https://graph.facebook.com/v21.0/{s.whatsapp_phone_number_id}/messages",
                    headers={"Authorization": f"Bearer {s.whatsapp_token}"},
                    json={"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}})
            result["sent"] = r.status_code < 400
            if not result["sent"]:
                result["error"] = r.text[:200]
        except httpx.HTTPError as exc:
            result["error"] = str(exc)
    return result
