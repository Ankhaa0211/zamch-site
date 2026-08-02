"""QPay V2 + eBarimt 3.0 merchant API client (centralized ЗАМЧ payments)."""
from __future__ import annotations

import os
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx

QPAY_BASE_URL = os.environ.get("QPAY_BASE_URL", "https://merchant.qpay.mn").rstrip("/")
QPAY_USERNAME = os.environ.get("QPAY_USERNAME", "")
QPAY_PASSWORD = os.environ.get("QPAY_PASSWORD", "")
QPAY_INVOICE_CODE = os.environ.get("QPAY_INVOICE_CODE", "")
QPAY_CALLBACK_BASE = os.environ.get(
    "QPAY_CALLBACK_BASE", os.environ.get("BASE_URL", "http://127.0.0.1:8000")
).rstrip("/")
QPAY_MOCK = os.environ.get("QPAY_MOCK", "").lower() in ("1", "true", "yes")

# eBarimt defaults (QPay merchant-д идэвхжүүлсний дараа ашиглана)
EBARIMT_ENABLED = os.environ.get("EBARIMT_ENABLED", "1").lower() in ("1", "true", "yes")
EBARIMT_TAX_TYPE = os.environ.get("EBARIMT_TAX_TYPE", "1")  # 1 | 2 | 3
EBARIMT_DISTRICT_CODE = os.environ.get("EBARIMT_DISTRICT_CODE", "34")
EBARIMT_CLASSIFICATION_CODE = os.environ.get("EBARIMT_CLASSIFICATION_CODE", "")
EBARIMT_TAX_PRODUCT_CODE = os.environ.get("EBARIMT_TAX_PRODUCT_CODE", "")

_token_cache: Dict[str, Any] = {"access_token": None, "expires_at": 0.0}


def qpay_configured() -> bool:
    """True when live merchant credentials are set (not mock-only)."""
    return bool(QPAY_USERNAME and QPAY_PASSWORD and QPAY_INVOICE_CODE) and not QPAY_MOCK


def is_mock_mode() -> bool:
    return QPAY_MOCK or not (QPAY_USERNAME and QPAY_PASSWORD and QPAY_INVOICE_CODE)


def normalize_receiver_type(value: str) -> str:
    """Map UI values to QPay eBarimt receiver type (CITIZEN/COMPANY preferred by eBarimt 3.0 docs)."""
    v = (value or "CITIZEN").strip().upper()
    if v in ("83", "CITIZEN", "INDIVIDUAL", "PERSON"):
        return "CITIZEN"
    if v in ("84", "COMPANY", "ORGANIZATION"):
        return "COMPANY"
    return "CITIZEN"


def _auth_header(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def get_access_token() -> str:
    now = time.time()
    if _token_cache["access_token"] and _token_cache["expires_at"] > now + 30:
        return _token_cache["access_token"]

    with httpx.Client(timeout=30.0) as client:
        res = client.post(
            f"{QPAY_BASE_URL}/v2/auth/token",
            auth=(QPAY_USERNAME, QPAY_PASSWORD),
        )
        if res.status_code >= 400:
            raise RuntimeError(f"QPay auth failed: {res.status_code} {res.text}")
        data = res.json()
        token = data.get("access_token")
        if not token:
            raise RuntimeError("QPay auth response missing access_token")
        expires_in = int(data.get("expires_in") or 600)
        _token_cache["access_token"] = token
        _token_cache["expires_at"] = now + expires_in
        return token


def create_invoice(
    *,
    sender_invoice_no: str,
    amount: float,
    description: str,
    callback_url: str,
    lines: Optional[List[Dict[str, Any]]] = None,
    district_code: Optional[str] = None,
    tax_type: Optional[str] = None,
) -> Dict[str, Any]:
    if is_mock_mode():
        invoice_id = f"mock-{uuid.uuid4()}"
        return {
            "invoice_id": invoice_id,
            "qr_text": f"MOCK-QPAY:{sender_invoice_no}:{int(amount)}",
            "qr_image": None,
            "qPay_shortUrl": f"{QPAY_CALLBACK_BASE}/payment/{sender_invoice_no}?mock=1",
            "urls": [],
            "mock": True,
            "amount": amount,
            "sender_invoice_no": sender_invoice_no,
        }

    token = get_access_token()
    payload: Dict[str, Any] = {
        "invoice_code": QPAY_INVOICE_CODE,
        "sender_invoice_no": sender_invoice_no,
        "invoice_receiver_code": "terminal",
        "invoice_description": description[:255],
        "amount": float(amount),
        "callback_url": callback_url,
    }

    # eBarimt 3.0 invoice tax fields
    if EBARIMT_ENABLED:
        payload["tax_type"] = tax_type or EBARIMT_TAX_TYPE
        payload["district_code"] = district_code or EBARIMT_DISTRICT_CODE
        invoice_lines = lines or []
        if not invoice_lines:
            invoice_lines = [
                {
                    "tax_product_code": EBARIMT_TAX_PRODUCT_CODE or "00000000",
                    "line_description": description[:100] or "ЗАМЧ захиалга",
                    "line_quantity": "1",
                    "line_unit_price": str(int(round(amount))),
                    "classification_code": EBARIMT_CLASSIFICATION_CODE or "",
                }
            ]
        payload["lines"] = invoice_lines

    with httpx.Client(timeout=30.0) as client:
        res = client.post(
            f"{QPAY_BASE_URL}/v2/invoice",
            headers=_auth_header(token),
            json=payload,
        )
        if res.status_code >= 400:
            raise RuntimeError(f"QPay invoice failed: {res.status_code} {res.text}")
        data = res.json()
        data["mock"] = False
        data["amount"] = amount
        data["sender_invoice_no"] = sender_invoice_no
        return data


def check_invoice_paid(invoice_id: str) -> Dict[str, Any]:
    """Returns {paid, paid_amount, payment_id, raw, mock}."""
    if str(invoice_id).startswith("mock-") or is_mock_mode():
        return {
            "paid": False,
            "paid_amount": 0,
            "payment_id": None,
            "raw": {"count": 0, "rows": []},
            "mock": True,
        }

    token = get_access_token()
    payload = {
        "object_type": "INVOICE",
        "object_id": invoice_id,
        "offset": {"page_number": 1, "page_limit": 10},
    }
    with httpx.Client(timeout=30.0) as client:
        res = client.post(
            f"{QPAY_BASE_URL}/v2/payment/check",
            headers=_auth_header(token),
            json=payload,
        )
        if res.status_code >= 400:
            res = client.post(
                f"{QPAY_BASE_URL}/v2/invoice/check",
                headers=_auth_header(token),
                json=payload,
            )
        if res.status_code >= 400:
            raise RuntimeError(f"QPay check failed: {res.status_code} {res.text}")
        data = res.json()
        count = int(data.get("count") or 0)
        paid_amount = float(data.get("paid_amount") or 0)
        rows = data.get("rows") or []
        paid = count > 0 or any(
            str(r.get("payment_status", "")).upper() == "PAID" for r in rows
        )
        payment_id = None
        for row in rows:
            if str(row.get("payment_status", "")).upper() == "PAID" or paid:
                payment_id = row.get("payment_id")
                if payment_id:
                    break
        if not payment_id and rows:
            payment_id = rows[0].get("payment_id")
        return {
            "paid": paid,
            "paid_amount": paid_amount,
            "payment_id": payment_id,
            "raw": data,
            "mock": False,
        }


def create_ebarimt(
    *,
    payment_id: str,
    receiver_type: str = "CITIZEN",
    receiver: str = "",
    district_code: Optional[str] = None,
) -> Dict[str, Any]:
    receiver_type = normalize_receiver_type(receiver_type)
    district = district_code or EBARIMT_DISTRICT_CODE

    if is_mock_mode() or str(payment_id).startswith("mock-"):
        return {
            "mock": True,
            "payment_id": payment_id,
            "ebarimt_receiver_type": receiver_type,
            "ebarimt_qr_data": f"MOCK-EBARIMT:{payment_id}",
            "ebarimt_lottery": "MOCK-LOTTO",
            "amount": "0",
            "vat_amount": "0",
            "barimt_status": "CREATED",
            "id": f"mock-ebarimt-{uuid.uuid4()}",
        }

    if not EBARIMT_ENABLED:
        return {"skipped": True, "reason": "EBARIMT_ENABLED=0"}

    token = get_access_token()
    payload = {
        "payment_id": payment_id,
        "ebarimt_receiver_type": receiver_type,
        "ebarimt_receiver": receiver if receiver_type == "COMPANY" else (receiver or ""),
        "district_code": district,
    }
    with httpx.Client(timeout=30.0) as client:
        res = client.post(
            f"{QPAY_BASE_URL}/v2/ebarimt_v3/create",
            headers=_auth_header(token),
            json=payload,
        )
        if res.status_code >= 400:
            raise RuntimeError(f"eBarimt create failed: {res.status_code} {res.text}")
        data = res.json()
        data["mock"] = False
        return data


def cancel_ebarimt(payment_id: str) -> Dict[str, Any]:
    if is_mock_mode() or str(payment_id).startswith("mock-"):
        return {"mock": True, "ok": True, "payment_id": payment_id}

    token = get_access_token()
    with httpx.Client(timeout=30.0) as client:
        res = client.delete(
            f"{QPAY_BASE_URL}/v2/ebarimt_v3/{payment_id}",
            headers=_auth_header(token),
        )
        if res.status_code >= 400:
            raise RuntimeError(f"eBarimt cancel failed: {res.status_code} {res.text}")
        try:
            return res.json() if res.content else {"ok": True}
        except Exception:
            return {"ok": True}


def callback_url_for(payment_group_id: str) -> str:
    return f"{QPAY_CALLBACK_BASE}/api/qpay/callback?payment_group_id={payment_group_id}"
