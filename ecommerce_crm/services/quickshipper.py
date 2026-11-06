"""Quickshipper Delivery API client.

Wraps the integrator-facing Delivery API documented at
https://app.theneo.io/quickshipper/delivery-en/quickshipper-delivery-api
(OpenAPI spec: https://delivery.quickshipper.app/swagger/v1/swagger.json).

Each ``QuickshipperClient`` instance is bound to one tenant's decrypted
API key. Methods map 1:1 to the endpoints we use from the storefront
checkout + dashboard:

- :py:meth:`get_quote` -> ``GET /v1/Order/fees`` (live shipping price at checkout)
- :py:meth:`get_weights` -> ``GET /v1/Order/weights`` (parcel-dimension lookup)
- :py:meth:`create_order` -> ``POST /v1/Order`` (book the courier; lands as Draft)
- :py:meth:`confirm_order` -> ``POST /v1/Order/confirm`` (Draft -> ReadyForPickup,
  i.e. actually hand the order to the courier provider)
- :py:meth:`get_order` -> ``GET /v1/Order`` (poll status as a webhook fallback)
- :py:meth:`change_status` -> ``POST /v1/Order/status`` (cancel / mark delivered)
- :py:meth:`delete_order` -> ``DELETE /v1/Order``
- :py:meth:`list_providers` -> ``GET /v1/Provider/list``
- :py:meth:`register_webhook` -> ``POST /v1/Webhook``
- :py:meth:`list_webhooks` -> ``GET /v1/Webhook``
- :py:meth:`account_info` -> ``GET /v1/Account``

Why we don't auto-OAuth: Quickshipper's portal hands integrators a single
long-lived API key. The auth header below sends it as ``Authorization:
Bearer <key>`` — that's the convention probed against the live service
during integration. If the convention turns out to require a different
header (the field name in the docs is also ``apiKey``), only
:py:meth:`_headers` needs to change.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Delivery API host. Quickshipper publishes only one production host for
# the integrator API; test API keys issued by their portal authenticate
# against the test auth server but still hit this delivery host (they
# just won't return real shop data). Override `base_url` per-client if
# Quickshipper ever spins up a dedicated sandbox host.
PRODUCTION_BASE_URL = "https://delivery.quickshipper.app"
SANDBOX_BASE_URL = "https://delivery.quickshipper.app"

# OAuth2 token endpoints. The Delivery API JWT is minted via a custom
# `Express` grant on Quickshipper's auth server: integrators present the
# shop's API key as `ApiKey`, and the auth server returns a Bearer JWT
# scoped to `DeliveryApi` and bound to that shop.
PRODUCTION_AUTH_TOKEN_URL = "https://auth.quickshipper.app/connect/token"
SANDBOX_AUTH_TOKEN_URL = "https://test-auth.quickshipper.ge/connect/token"

# These are public dashboard credentials extracted from the storefront
# SPA — every Quickshipper integrator passes them. The shop-specific
# auth bit comes from the per-tenant `ApiKey` parameter.
OAUTH_CLIENT_ID = "QuickShipperClient"
OAUTH_CLIENT_SECRET = "QuickShipperSecret"
OAUTH_SCOPE = "DeliveryApi"

DEFAULT_TIMEOUT_SECONDS = 15
# Refresh the JWT 60s before its declared expiry so a token that's about
# to expire mid-call gets re-minted instead of the request 401-ing.
TOKEN_EXPIRY_BUFFER_SECONDS = 60


class QuickshipperError(Exception):
    """Raised when the Delivery API rejects a request or returns success=False."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        errors: list[str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.errors = errors or []
        self.payload = payload or {}


class QuickshipperClient:
    """Thin requests-based wrapper around the Delivery API."""

    def __init__(
        self,
        api_key: str,
        *,
        use_production: bool = False,
        base_url: str | None = None,
        token_url: str | None = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        session: requests.Session | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Quickshipper API key is required")
        self.api_key = api_key
        self.use_production = use_production
        self.base_url = base_url or (PRODUCTION_BASE_URL if use_production else SANDBOX_BASE_URL)
        self.token_url = token_url or (
            PRODUCTION_AUTH_TOKEN_URL if use_production else SANDBOX_AUTH_TOKEN_URL
        )
        self.timeout = timeout
        self.session = session or requests.Session()
        # JWT cache. Tokens are valid for ~1 year, so a single mint
        # comfortably outlasts every request the worker handles.
        self._access_token: str | None = None
        self._access_token_expires_at: float = 0.0

    # ---- auth ---------------------------------------------------------------

    def _mint_access_token(self) -> str:
        """Exchange the per-tenant API key for a Delivery API JWT via the
        ``Express`` OAuth2 grant. The JWT carries the shop binding the
        Delivery API uses to scope every subsequent request."""
        try:
            response = self.session.post(
                self.token_url,
                data={
                    "grant_type": "Express",
                    "client_id": OAUTH_CLIENT_ID,
                    "client_secret": OAUTH_CLIENT_SECRET,
                    "scope": OAUTH_SCOPE,
                    "ApiKey": self.api_key,
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            logger.warning("Quickshipper token mint network error: %s", exc)
            raise QuickshipperError(
                f"Network error contacting Quickshipper auth: {exc}"
            ) from exc

        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = {}

        if response.status_code >= 400 or not data.get("access_token"):
            err = data.get("error_description") or data.get("error") or f"HTTP {response.status_code}"
            raise QuickshipperError(
                f"Quickshipper auth rejected the API key: {err}",
                status_code=response.status_code,
                payload=data if isinstance(data, dict) else {},
            )

        self._access_token = data["access_token"]
        # Token endpoint reports `expires_in` in seconds; treat anything
        # under the buffer as already expired so we don't cache it.
        expires_in = int(data.get("expires_in", 0))
        if expires_in > TOKEN_EXPIRY_BUFFER_SECONDS:
            self._access_token_expires_at = time.time() + expires_in - TOKEN_EXPIRY_BUFFER_SECONDS
        else:
            self._access_token_expires_at = 0.0
        return self._access_token

    def _get_access_token(self) -> str:
        if self._access_token and time.time() < self._access_token_expires_at:
            return self._access_token
        return self._mint_access_token()

    # ---- low-level helpers --------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_access_token()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            response = self.session.request(
                method,
                url,
                params=_clean_params(params) if params else None,
                json=json,
                headers=self._headers(),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            logger.warning("Quickshipper %s %s network error: %s", method, path, exc)
            raise QuickshipperError(f"Network error contacting Quickshipper: {exc}") from exc

        # The API returns BaseResponseModel-shaped envelopes for almost every
        # endpoint, with `success` + `errors`. We surface a structured error
        # if the request failed at the HTTP layer OR at the application layer.
        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = {"raw": response.text}

        if response.status_code >= 400:
            errors = data.get("errors") if isinstance(data, dict) else None
            user_message = data.get("userMessage") if isinstance(data, dict) else None
            developer_message = data.get("developerMessage") if isinstance(data, dict) else None
            message = user_message or developer_message or f"HTTP {response.status_code}"
            raise QuickshipperError(
                message,
                status_code=response.status_code,
                errors=errors or [],
                payload=data if isinstance(data, dict) else {},
            )

        if isinstance(data, dict) and data.get("success") is False:
            errors = data.get("errors") or []
            message = data.get("userMessage") or data.get("developerMessage") or "Quickshipper request failed"
            raise QuickshipperError(
                message,
                status_code=response.status_code,
                errors=errors,
                payload=data,
            )

        return data if isinstance(data, dict) else {"data": data}

    # ---- account & providers ------------------------------------------------

    def account_info(self) -> dict[str, Any]:
        """``GET /v1/Account`` — verifies credentials and returns the shop's
        Quickshipper profile (shop name, logo, balance, configured providers).
        Used by the admin "Test Connection" button."""
        return self._request("GET", "/v1/Account")

    def list_providers(self) -> list[dict[str, Any]]:
        """``GET /v1/Provider/list`` — couriers configured for this shop."""
        data = self._request("GET", "/v1/Provider/list")
        return data.get("items") or []

    # ---- quotes -------------------------------------------------------------

    def get_quote(
        self,
        *,
        from_lat: float | Decimal,
        from_lng: float | Decimal,
        from_street: str | None = None,
        from_city: str | None = None,
        to_lat: float | Decimal,
        to_lng: float | Decimal,
        to_street: str | None = None,
        to_city: str | None = None,
        cart_amount: float | Decimal | None = None,
        cart_weight: float | Decimal | None = None,
        car_delivery: bool | None = None,
        scheduled_provider_id: int | None = None,
        scheduled_date: str | None = None,
    ) -> dict[str, Any]:
        """``GET /v1/Order/fees`` — list of provider fees for this shipment.

        Returns the raw response envelope so callers can pick the cheapest /
        fastest fee + the matching provider.
        """
        params = {
            "FromLatitude": from_lat,
            "FromLongitude": from_lng,
            "FromStreetName": from_street,
            "FromCityName": from_city,
            "ToLatitude": to_lat,
            "ToLongitude": to_lng,
            "ToStreetName": to_street,
            "ToCityName": to_city,
            "CartAmount": cart_amount,
            "CartWeight": cart_weight,
            "CarDelivery": car_delivery,
            "ScheduleDelivery.ProviderId": scheduled_provider_id,
            "ScheduleDelivery.ScheduledDate": scheduled_date,
        }
        return self._request("GET", "/v1/Order/fees", params=params)

    def get_weights(
        self,
        *,
        from_lat: float | Decimal | None = None,
        from_lng: float | Decimal | None = None,
        to_lat: float | Decimal | None = None,
        to_lng: float | Decimal | None = None,
        provider_id: int | None = None,
        delivery_speed_id: int | None = None,
        cart_amount: float | Decimal | None = None,
    ) -> dict[str, Any]:
        """``GET /v1/Order/weights`` — list of parcel-dimension presets the
        provider supports for this route. Each entry has a
        ``parcelDimensionsId`` we feed back into :py:meth:`create_order`."""
        params = {
            "FromLatitude": from_lat,
            "FromLongitude": from_lng,
            "ToLatitude": to_lat,
            "ToLongitude": to_lng,
            "ProviderId": provider_id,
            "DeliverySpeedId": delivery_speed_id,
            "CartAmount": cart_amount,
        }
        return self._request("GET", "/v1/Order/weights", params=params)

    # ---- orders -------------------------------------------------------------

    def create_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        """``POST /v1/Order`` — book a courier.

        Caller is responsible for assembling the OrderPlaceRequestModel. Fields
        we always send (and which the swagger marks nullable):

        - ``pickUpInfo``: address + lat/lng + name + phone (from EcommerceSettings)
        - ``dropOffInfo``: same shape, taken from Order.delivery_address
        - ``parcels``: list of OrderParcelModel
        - ``provider.providerId`` + ``provider.providerFeeId`` from the chosen quote
        - ``parcelDimensionId`` from the chosen weight bucket
        - ``integrationOrderId``: our Order.order_number for back-reference
        - ``cartAmount`` for COD support
        """
        return self._request("POST", "/v1/Order", json=payload)

    def confirm_order(self, order_id: int) -> dict[str, Any]:
        """``POST /v1/Order/confirm`` — forward a Draft order to its courier
        provider (status becomes ReadyForPickup). ``create_order`` alone
        leaves the order as a Draft in the shop's Quickshipper dashboard."""
        return self._request("POST", "/v1/Order/confirm", json={"orderId": int(order_id)})

    def get_order(self, order_id: int) -> dict[str, Any]:
        """``GET /v1/Order?OrderId=<id>`` — fallback when a webhook is missed."""
        return self._request("GET", "/v1/Order", params={"OrderId": order_id})

    def change_status(
        self,
        *,
        order_id: int,
        status: str,
        comment: str | None = None,
    ) -> dict[str, Any]:
        """``POST /v1/Order/status`` — cancel or override an order's status."""
        return self._request(
            "POST",
            "/v1/Order/status",
            json={"orderId": order_id, "status": status, "orderComment": comment},
        )

    def delete_order(self, order_id: int) -> dict[str, Any]:
        """``DELETE /v1/Order`` — only allowed for orders we created."""
        return self._request("DELETE", "/v1/Order", json={"orderId": order_id})

    # ---- webhooks -----------------------------------------------------------

    def list_webhooks(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/v1/Webhook")
        return data.get("webhooks") or []

    def register_webhook(
        self,
        *,
        callback_url: str,
        webhook_type: str = "OrderStatus",
        max_retry_count: int = 5,
    ) -> dict[str, Any]:
        """``POST /v1/Webhook``. ``webhook_type`` corresponds to the
        ``WebhookType`` enum on Quickshipper (OrderStatus, etc.)."""
        return self._request(
            "POST",
            "/v1/Webhook",
            json={
                "callBackUrl": callback_url,
                "type": webhook_type,
                "maxRetryCount": max_retry_count,
            },
        )

    def edit_webhook(
        self,
        *,
        webhook_id: int,
        callback_url: str | None = None,
        is_active: bool | None = None,
        max_retry_count: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"id": webhook_id}
        if callback_url is not None:
            body["callBackUrl"] = callback_url
        if is_active is not None:
            body["isActive"] = is_active
        if max_retry_count is not None:
            body["maxRetryCount"] = max_retry_count
        return self._request("PUT", "/v1/Webhook", json=body)


def _clean_params(params: dict[str, Any]) -> dict[str, Any]:
    """Drop ``None`` values so we don't send empty query keys."""
    out: dict[str, Any] = {}
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, Decimal):
            v = float(v)
        if isinstance(v, bool):
            v = "true" if v else "false"
        out[k] = v
    return out


def client_from_settings(settings_obj) -> QuickshipperClient | None:
    """Build a client from an :class:`ecommerce_crm.models.EcommerceSettings`
    row. Returns ``None`` if Quickshipper isn't configured."""
    if not settings_obj or not settings_obj.has_quickshipper_credentials:
        return None
    api_key = settings_obj.get_quickshipper_api_key()
    if not api_key:
        return None
    return QuickshipperClient(
        api_key,
        use_production=settings_obj.quickshipper_use_production,
    )
