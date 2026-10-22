"""Outbound email + SMS for onboarding verification.

Unconfigured or failed providers return False. Message contents, verification
codes, recipient details and provider response bodies are never written to logs.
Development tests read synthetic codes from their disposable database.
"""
from __future__ import annotations

import logging

import httpx

from config import settings

log = logging.getLogger("ollie.notify")


def send_email(to: str, subject: str, html: str, text: str | None = None) -> bool:
    """Send an email via Resend. Returns True if handed off to the provider.
    With no RESEND_API_KEY set, returns False without logging message contents."""
    key = settings.resend_api_key
    if not key:
        log.warning("Email delivery unavailable: provider is not configured")
        return False
    try:
        r = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"from": settings.email_from, "to": [to], "subject": subject, "html": html},
            timeout=15,
        )
        if r.status_code >= 300:
            log.error("Email provider rejected delivery: HTTP %s", r.status_code)
            return False
        return True
    except Exception as e:                       # never let email break the request
        log.error("Email delivery failed: %s", type(e).__name__)
        return False


def send_sms(to: str, body: str) -> bool:
    """Send an SMS via Twilio. Returns True if handed off to the provider.
    Without provider configuration, returns False without logging the message."""
    sid, token, frm = settings.twilio_account_sid, settings.twilio_auth_token, settings.twilio_from_number
    if not (sid and token and frm):
        log.warning("SMS delivery unavailable: provider is not configured")
        return False
    try:
        r = httpx.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            data={"To": to, "From": frm, "Body": body},
            auth=(sid, token),
            timeout=15,
        )
        if r.status_code >= 300:
            log.error("SMS provider rejected delivery: HTTP %s", r.status_code)
            return False
        return True
    except Exception as e:
        log.error("SMS delivery failed: %s", type(e).__name__)
        return False


def verification_email_html(code: str, minutes: int) -> str:
    return (
        f'<div style="font-family:system-ui,sans-serif;max-width:440px;margin:0 auto">'
        f'<h2 style="color:#0A8754">Confirm your email</h2>'
        f'<p>Your Apex Property verification code is:</p>'
        f'<div style="font-size:32px;font-weight:700;letter-spacing:.2em;'
        f'background:#f3f4f6;border-radius:12px;padding:16px;text-align:center">{code}</div>'
        f'<p style="color:#6b7280;font-size:13px">This code expires in {minutes} minutes.</p>'
        f'</div>'
    )
