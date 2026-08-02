"""
Delivery integration layer (connection prep only — no driver app).

Providers are separate so ЗАМЧ can switch later without rewriting orders:
  - manual   : дэлгүүр/оператор гараар удирдана (одоогийн default)
  - own_app  : ирээдүйн ЗАМЧ delivery app (webhook + job payload бэлэн)
  - partner  : гадны delivery компани (тусдаа API adapter)

App өөрөө энд байхгүй — зөвхөн marketplace → provider хоорондын гэрээ/холболт.
"""
from __future__ import annotations

import os
import uuid
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

import httpx

# Active provider: manual | own_app | partner
DELIVERY_PROVIDER = os.environ.get("DELIVERY_PROVIDER", "manual").strip().lower()

# Own app (future)
OWN_APP_WEBHOOK_URL = os.environ.get("OWN_APP_WEBHOOK_URL", "").rstrip("/")
OWN_APP_API_KEY = os.environ.get("OWN_APP_API_KEY", "")

# External partner (Plan B)
PARTNER_NAME = os.environ.get("DELIVERY_PARTNER_NAME", "partner")
PARTNER_API_URL = os.environ.get("DELIVERY_PARTNER_API_URL", "").rstrip("/")
PARTNER_API_KEY = os.environ.get("DELIVERY_PARTNER_API_KEY", "")

# Shared secret for inbound status webhooks from either provider
DELIVERY_WEBHOOK_SECRET = os.environ.get("DELIVERY_WEBHOOK_SECRET", "zamch-delivery-dev-secret")


def active_provider_name() -> str:
    name = DELIVERY_PROVIDER
    if name not in ("manual", "own_app", "partner"):
        return "manual"
    return name


def build_job_payload(shipment: Dict[str, Any]) -> Dict[str, Any]:
    """Canonical payload both own_app and partner adapters understand."""
    return {
        "shipment_id": shipment.get("id"),
        "external_ref": shipment.get("external_ref"),
        "order_id": shipment.get("order_id"),
        "store_id": shipment.get("store_id"),
        "provider": shipment.get("provider"),
        "pickup": {
            "name": shipment.get("pickup_name"),
            "phone": shipment.get("pickup_phone"),
            "address": shipment.get("pickup_address"),
            "location": shipment.get("pickup_location"),
        },
        "dropoff": {
            "name": shipment.get("customer_name"),
            "phone": shipment.get("customer_phone"),
            "address": shipment.get("dropoff_address"),
        },
        "cod_amount": shipment.get("cod_amount") or 0,
        "note": shipment.get("note"),
        "items_summary": shipment.get("items_summary"),
        "total": shipment.get("order_total"),
    }


class DeliveryProvider(ABC):
    name: str

    @abstractmethod
    def create_job(self, shipment: Dict[str, Any]) -> Dict[str, Any]:
        """Send job to provider. Returns {ok, provider_job_id, raw, mode}."""

    def cancel_job(self, provider_job_id: str) -> Dict[str, Any]:
        return {"ok": True, "skipped": True}


class ManualProvider(DeliveryProvider):
    name = "manual"

    def create_job(self, shipment: Dict[str, Any]) -> Dict[str, Any]:
        # No external call — operator handles in seller panel later
        return {
            "ok": True,
            "mode": "manual",
            "provider_job_id": f"manual-{shipment.get('id') or uuid.uuid4().hex[:8]}",
            "raw": {"message": "Гараар хүргэлт — дэлгүүр/оператор бэлдэнэ"},
        }


class OwnAppProvider(DeliveryProvider):
    """Future ЗАМЧ delivery app — POST job to OWN_APP_WEBHOOK_URL when set."""

    name = "own_app"

    def create_job(self, shipment: Dict[str, Any]) -> Dict[str, Any]:
        payload = build_job_payload(shipment)
        if not OWN_APP_WEBHOOK_URL:
            # App not ready: queue locally, mark pending_dispatch
            return {
                "ok": True,
                "mode": "queued_local",
                "provider_job_id": f"own-pending-{uuid.uuid4().hex[:10]}",
                "raw": {
                    "message": "Own app URL тохируулаагүй — job дотоодд хүлээгдэж байна",
                    "payload": payload,
                },
            }

        headers = {"Content-Type": "application/json"}
        if OWN_APP_API_KEY:
            headers["Authorization"] = f"Bearer {OWN_APP_API_KEY}"
        with httpx.Client(timeout=20.0) as client:
            res = client.post(f"{OWN_APP_WEBHOOK_URL}/jobs", headers=headers, json=payload)
            if res.status_code >= 400:
                raise RuntimeError(f"Own app job failed: {res.status_code} {res.text}")
            data = res.json() if res.content else {}
            return {
                "ok": True,
                "mode": "pushed",
                "provider_job_id": str(data.get("job_id") or data.get("id") or uuid.uuid4()),
                "raw": data,
            }


class PartnerProvider(DeliveryProvider):
    """External delivery company (Plan B) — separate adapter from own_app."""

    name = "partner"

    def create_job(self, shipment: Dict[str, Any]) -> Dict[str, Any]:
        payload = build_job_payload(shipment)
        payload["partner"] = PARTNER_NAME
        if not PARTNER_API_URL:
            return {
                "ok": True,
                "mode": "queued_local",
                "provider_job_id": f"partner-pending-{uuid.uuid4().hex[:10]}",
                "raw": {
                    "message": "Partner API URL тохируулаагүй — job дотоодд хүлээгдэж байна",
                    "partner": PARTNER_NAME,
                    "payload": payload,
                },
            }

        headers = {"Content-Type": "application/json"}
        if PARTNER_API_KEY:
            headers["Authorization"] = f"Bearer {PARTNER_API_KEY}"
        with httpx.Client(timeout=20.0) as client:
            res = client.post(f"{PARTNER_API_URL}/deliveries", headers=headers, json=payload)
            if res.status_code >= 400:
                raise RuntimeError(f"Partner job failed: {res.status_code} {res.text}")
            data = res.json() if res.content else {}
            return {
                "ok": True,
                "mode": "pushed",
                "provider_job_id": str(
                    data.get("tracking_id") or data.get("job_id") or data.get("id") or uuid.uuid4()
                ),
                "raw": data,
            }


def get_provider(name: Optional[str] = None) -> DeliveryProvider:
    key = (name or active_provider_name()).lower()
    if key == "own_app":
        return OwnAppProvider()
    if key == "partner":
        return PartnerProvider()
    return ManualProvider()


def provider_status() -> Dict[str, Any]:
    active = active_provider_name()
    return {
        "active_provider": active,
        "providers": {
            "manual": {"ready": True, "description": "Гараар / оператор"},
            "own_app": {
                "ready": bool(OWN_APP_WEBHOOK_URL),
                "webhook_configured": bool(OWN_APP_WEBHOOK_URL),
                "description": "ЗАМЧ delivery app (ирээдүй)",
            },
            "partner": {
                "ready": bool(PARTNER_API_URL),
                "name": PARTNER_NAME,
                "api_configured": bool(PARTNER_API_URL),
                "description": "Гадны delivery компани (Plan B)",
            },
        },
        "webhook_secret_configured": bool(DELIVERY_WEBHOOK_SECRET),
    }
