"""SMS sending for OTP. Mock by default; plug in a provider when credentials exist."""

from __future__ import annotations

import os
import urllib.error
import urllib.request


SMS_PROVIDER = (os.getenv("SMS_PROVIDER") or "mock").strip().lower()
SMS_API_URL = (os.getenv("SMS_API_URL") or "").strip()
SMS_API_KEY = (os.getenv("SMS_API_KEY") or "").strip()
SMS_SENDER = (os.getenv("SMS_SENDER") or "ZAMCH").strip()


def send_sms(phone: str, message: str) -> None:
    """Send SMS. Mock provider only logs. HTTP providers POST JSON to SMS_API_URL."""
    phone = phone.strip()
    if SMS_PROVIDER in ("", "mock", "dev", "none"):
        print(f"[SMS mock] to={phone} sender={SMS_SENDER} msg={message}")
        return

    if not SMS_API_URL:
        raise RuntimeError("SMS_API_URL тохируулаагүй байна")

    payload = (
        '{"to":"%s","from":"%s","text":"%s"}'
        % (phone.replace('"', ""), SMS_SENDER.replace('"', ""), message.replace('"', '\\"'))
    ).encode("utf-8")
    req = urllib.request.Request(
        SMS_API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {SMS_API_KEY}" if SMS_API_KEY else "",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if getattr(resp, "status", 200) >= 400:
                raise RuntimeError(f"SMS provider status {resp.status}")
    except urllib.error.URLError as exc:
        raise RuntimeError("SMS илгээж чадсангүй") from exc
