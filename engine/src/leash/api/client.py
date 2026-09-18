"""HTTP client for the Viseca sandbox.

Thin on purpose. It knows the endpoints, the bearer key and the error shape,
and nothing about decisions. Everything it returns is handed to the parser
before the engine sees it, so a malformed response fails here rather than
becoming a half-understood purchase.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from typing_extensions import Self

from ..config import Settings


class ApiError(RuntimeError):
    """The sandbox returned an error, or could not be reached."""

    def __init__(self, status: int | None, message: str, payload: Any = None):
        self.status = status
        self.payload = payload
        super().__init__(f"[{status or 'network'}] {message}")


@dataclass
class Polled:
    """One long-poll result. `envelope` is None when the server had no work."""

    envelope: dict | None

    @property
    def has_work(self) -> bool:
        return self.envelope is not None


class LeashClient:
    """Calls the sandbox. One instance per team key."""

    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None):
        self.settings = settings or Settings.from_env()
        self._client = client or httpx.Client(timeout=30.0)

    # --- plumbing ----------------------------------------------------------

    def _headers(self, *, keyed: bool = True) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if keyed:
            if not self.settings.api_key:
                raise ApiError(None, "TEAM_API_KEY is not set; keyed endpoints are unavailable")
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
        keyed: bool = True,
        timeout: float | None = None,
    ) -> httpx.Response:
        url = f"{self.settings.base_url}{path}"
        try:
            response = self._client.request(
                method,
                url,
                json=json_body,
                params=params,
                headers=self._headers(keyed=keyed),
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise ApiError(None, f"{method} {path}: {exc}") from exc

        # "Check the HTTP status before treating a response as a successful
        # result." An error body is JSON under `error`.
        if response.status_code >= 400:
            try:
                payload = response.json()
                message = payload.get("error", payload)
            except ValueError:
                payload, message = None, response.text[:400]
            raise ApiError(response.status_code, f"{method} {path}: {message}", payload)
        return response

    @staticmethod
    def _json(response: httpx.Response) -> dict:
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(response.status_code, f"response was not JSON: {exc}") from exc

    # --- reading -----------------------------------------------------------

    def healthz(self) -> dict:
        """Service health. The only endpoint that needs no key."""
        return self._json(self._request("GET", "/healthz", keyed=False))

    def bootstrap(self) -> dict:
        """Versions, scenarios, limits, timeout settings and features."""
        return self._json(self._request("GET", "/v1/bootstrap"))

    def reference_data(self) -> dict:
        return self._json(self._request("GET", "/v1/reference-data"))

    def authorization_history_csv(self) -> str:
        response = self._request("GET", "/v1/reference-data/authorization-history.csv")
        return response.text

    # --- the mandate -------------------------------------------------------

    def create_mandate(self, draft: dict) -> dict:
        """Store the instruction and our rules as a draft. Returns `draft_id`.

        Customer, card and profile IDs are never submitted; the platform
        assigns them when the run starts.
        """
        for forbidden in ("customer_id", "card_id", "profile_id", "mandate_id"):
            if forbidden in draft:
                raise ApiError(None, f"a mandate draft must not carry {forbidden}")
        return self._json(self._request("POST", "/v1/mandates", json_body=draft))

    def confirm_mandate(self, draft_id: str) -> dict:
        """Record the customer's agreement. Returns `mandate_id`."""
        return self._json(
            self._request(
                "POST", f"/v1/mandates/{draft_id}/confirm", json_body={"confirmed": True}
            )
        )

    def get_mandate(self, mandate_id: str) -> dict:
        return self._json(self._request("GET", f"/v1/mandates/{mandate_id}"))

    def patch_mandate(self, mandate_id: str, changes: dict) -> dict:
        """Tighten an active mandate. Omitted fields stay unchanged."""
        return self._json(
            self._request("PATCH", f"/v1/mandates/{mandate_id}", json_body=changes)
        )

    def revoke_mandate(self, mandate_id: str) -> dict:
        """Withdraw permission."""
        return self._json(self._request("DELETE", f"/v1/mandates/{mandate_id}"))

    # --- runs --------------------------------------------------------------

    def start_run(self, scenario_id: str, mandate_id: str) -> dict:
        return self._json(
            self._request(
                "POST",
                "/v1/scenario-runs",
                json_body={"scenario_id": scenario_id, "mandate_id": mandate_id},
            )
        )

    def run_progress(self, run_id: str) -> dict:
        return self._json(self._request("GET", f"/v1/scenario-runs/{run_id}"))

    def next_request(self, wait: int = 25) -> Polled:
        """Long-poll for work.

        `204` means the server had nothing to hand over right now. It does not
        mean the run has finished, and it carries no body to parse.
        """
        response = self._request(
            "GET",
            "/v1/decision-requests/next",
            params={"wait": wait},
            timeout=wait + 10,
        )
        if response.status_code == 204:
            return Polled(None)
        return Polled(self._json(response))

    # --- answering ---------------------------------------------------------

    def submit_decision(self, authorization_id: str, payload: dict) -> dict:
        return self._json(
            self._request(
                "POST", f"/v1/authorizations/{authorization_id}/decision", json_body=payload
            )
        )

    def resolve(self, authorization_id: str, payload: dict) -> dict:
        """Record a real customer's answer after a step_up.

        Never used to send a second automated decision.
        """
        return self._json(
            self._request(
                "POST", f"/v1/authorizations/{authorization_id}/resolve", json_body=payload
            )
        )

    def authorizations(self) -> dict:
        return self._json(self._request("GET", "/v1/authorizations"))

    def events(self, since: int = 0) -> dict:
        return self._json(self._request("GET", "/v1/events", params={"since": since}))

    def reset_team(self) -> dict:
        """Clear team development state. Disabled during judging."""
        return self._json(self._request("POST", "/v1/team/reset"))

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
