"""HTTP client for the ERP: timeouts, retries on transient failures, idempotent writes."""

import time
from collections.abc import Callable
from typing import Any

import httpx


class ERPError(Exception):
    """The ERP answered with a definite refusal (4xx). Retrying will not help."""


class ERPUnavailable(Exception):
    """The ERP could not be reached, or kept failing (5xx), after every retry."""


class ERPClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        client: httpx.Client | None = None,
        retries: int = 3,
        backoff: float = 0.3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.client = client or httpx.Client(timeout=10.0)
        self.retries = retries
        self.backoff = backoff
        self.sleep = sleep

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {"X-ERP-Token": self.token, **kwargs.pop("headers", {})}
        last: Exception | str = "aucune tentative"
        for attempt in range(self.retries):
            if attempt:
                self.sleep(self.backoff * 2 ** (attempt - 1))
            try:
                resp = self.client.request(
                    method, f"{self.base_url}{path}", headers=headers, **kwargs
                )
            except httpx.TransportError as exc:
                last = exc
                continue
            if resp.status_code >= 500:
                last = f"HTTP {resp.status_code}"
                continue
            return resp
        raise ERPUnavailable(f"ERP indisponible après {self.retries} tentatives : {last}")

    @staticmethod
    def _json_or_raise(resp: httpx.Response) -> dict:
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise ERPError(str(detail))
        return resp.json()

    def find_customer(self, email: str) -> dict | None:
        resp = self._request("GET", "/customers", params={"email": email})
        return None if resp.status_code == 404 else self._json_or_raise(resp)

    def get_order(self, ref: str) -> dict | None:
        resp = self._request("GET", f"/orders/{ref}")
        return None if resp.status_code == 404 else self._json_or_raise(resp)

    def create_refund(self, order_ref: str, amount_cents: int, reason: str, key: str) -> dict:
        # The idempotency key makes a retried POST safe: the ERP replays the first result.
        resp = self._request(
            "POST",
            "/refunds",
            json={"order_ref": order_ref, "amount_cents": amount_cents, "reason": reason},
            headers={"Idempotency-Key": key},
        )
        return self._json_or_raise(resp)

    def create_escalation(self, team: str, reason: str, customer_email: str, key: str) -> dict:
        resp = self._request(
            "POST",
            "/escalations",
            json={"team": team, "reason": reason, "customer_email": customer_email},
            headers={"Idempotency-Key": key},
        )
        return self._json_or_raise(resp)
