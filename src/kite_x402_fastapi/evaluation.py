"""Deterministic x402 conformance checks for dependency-injected FastAPI services.

The runner exercises the real x402 middleware and facilitator client while
replacing facilitator and upstream HTTP traffic with scripted mock transports.
It is intended to check application behavior without contacting a live
facilitator or settling real payments.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from x402.http import FacilitatorConfig, HTTPFacilitatorClient

from .app import create_app
from .config import Settings
from .kite import KITE_MAINNET, KITE_TESTNET, KiteChain, kite_chain_by_name

PAY_TO = "0x1111111111111111111111111111111111111111"
PAYER = "0x2222222222222222222222222222222222222222"
TX = "0x" + "ab" * 32
DEFAULT_UPSTREAM_URL = "https://upstream.example/base"


def decode(value: str) -> dict[str, Any]:
    """Decode the SDK's base64-encoded JSON HTTP headers."""
    return json.loads(base64.b64decode(value))


def _json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return {"base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


class MockNetwork:
    """Recorded facilitator and upstream responses used by tests and the CLI."""

    def __init__(self, chain: KiteChain = KITE_MAINNET):
        self.chain = chain
        self.events: list[str] = []
        self.facilitator_requests: list[httpx.Request] = []
        self.upstream_requests: list[httpx.Request] = []
        self.verify_valid = True
        self.settle_success = True
        self.bad_response_at: str | None = None
        self.transport_error_at: str | None = None
        self.upstream_status = 200
        self.upstream_body = b'{"hello":"Kite"}'
        self.upstream_headers = {"content-type": "application/json", "x-upstream": "yes"}

    def facilitator(self, request: httpx.Request) -> httpx.Response:
        operation = request.url.path.rsplit("/", 1)[-1]
        assert request.url.host == "facilitator.example"
        assert request.url.path == f"/v2/{operation}"
        self.events.append(operation)
        self.facilitator_requests.append(request)
        if operation == self.transport_error_at:
            raise httpx.ConnectError("connection refused", request=request)
        if operation == self.bad_response_at:
            return httpx.Response(200, text="not JSON")
        if operation == "supported":
            assert request.method == "GET"
            return httpx.Response(200, json={
                "kinds": [{"x402Version": 2, "scheme": "exact", "network": self.chain.network}],
                "extensions": [], "signers": {},
            })
        assert request.method == "POST"
        body = json.loads(request.content)
        assert body["x402Version"] == 2
        assert body["paymentRequirements"] == body["paymentPayload"]["accepted"]
        if operation == "verify":
            return httpx.Response(200, json={
                "isValid": self.verify_valid, "payer": PAYER,
                **({} if self.verify_valid else {"invalidReason": "invalid_signature"}),
            })
        assert operation == "settle"
        assert self.events[-2] == "upstream", "Settlement must follow upstream success"
        return httpx.Response(200, json={
            "success": self.settle_success, "network": self.chain.network,
            "transaction": TX if self.settle_success else "", "payer": PAYER,
            **({} if self.settle_success else {"errorReason": "insufficient_funds"}),
        })

    def upstream(self, request: httpx.Request) -> httpx.Response:
        assert self.events[-1] in ("verify", "upstream")
        self.events.append("upstream")
        self.upstream_requests.append(request)
        if self.transport_error_at == "upstream":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(self.upstream_status, content=self.upstream_body, headers=self.upstream_headers)


class _MockHTTPFacilitatorClient(HTTPFacilitatorClient):
    """Use the same mock transport for the SDK's synchronous /supported call."""

    def __init__(self, config: FacilitatorConfig, transport: httpx.MockTransport):
        super().__init__(config)
        self._evaluation_transport = transport

    def _get_sync_client(self) -> httpx.Client:
        return httpx.Client(transport=self._evaluation_transport)


@contextmanager
def running(
    monkeypatch: Any = None,
    network: MockNetwork | None = None,
    *,
    settings: Settings | None = None,
    app_factory: Callable[..., FastAPI] = create_app,
    **overrides: Any,
) -> Iterator[tuple[TestClient, MockNetwork]]:
    """Run the existing app with recorded mock HTTP dependencies.

    ``monkeypatch`` remains an optional first argument for compatibility with
    the existing pytest fixtures; the transport no longer needs global patching.
    """
    del monkeypatch
    network = network or MockNetwork()
    facilitator_transport = httpx.MockTransport(network.facilitator)
    facilitator_http = httpx.AsyncClient(transport=facilitator_transport)
    upstream_http = httpx.AsyncClient(transport=httpx.MockTransport(network.upstream))
    facilitator = _MockHTTPFacilitatorClient(
        FacilitatorConfig(url="https://facilitator.example/v2", http_client=facilitator_http),
        facilitator_transport,
    )
    if settings is None:
        settings = Settings(
            pay_to=PAY_TO,
            upstream_url=DEFAULT_UPSTREAM_URL,
            kite_network="testnet" if network.chain == KITE_TESTNET else "mainnet",
            **overrides,
        )
    try:
        with TestClient(app_factory(settings=settings, facilitator=facilitator, upstream_client=upstream_http)) as client:
            assert network.events == ["supported"]
            network.events.clear()
            yield client, network
    finally:
        asyncio.run(facilitator_http.aclose())
        asyncio.run(upstream_http.aclose())


def payment_header(
    client: TestClient,
    path: str = "/v1/hello",
    *,
    mutate: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, str]:
    """Build the synthetic payload used by the shared tests and evaluation."""
    challenge = decode(client.get(path).headers["payment-required"])
    accepted = challenge["accepts"][0]
    payload: dict[str, Any] = {
        "x402Version": 2, "accepted": accepted,
        "resource": challenge["resource"],
        "payload": {
            "signature": "0x" + "00" * 65,
            "authorization": {
                "from": PAYER, "to": accepted["payTo"], "value": accepted["amount"],
                "validAfter": "0", "validBefore": "9999999999", "nonce": "0x" + "01" * 32,
            },
        },
    }
    if mutate is not None:
        mutate(payload)
    return {"PAYMENT-SIGNATURE": base64.b64encode(json.dumps(payload).encode()).decode()}


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    expected: Any
    actual: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": "passed" if self.passed else "failed",
            "expected": _json_safe(self.expected),
            "actual": _json_safe(self.actual),
        }


@dataclass(frozen=True)
class EvaluationCase:
    id: str
    name: str
    checks: tuple[Check, ...]
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(check.passed for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": "passed" if self.passed else "failed",
            "checks": [check.to_dict() for check in self.checks],
            **({"error": self.error} if self.error else {}),
        }


@dataclass(frozen=True)
class EvaluationReport:
    network: str
    asset: str
    price: str
    cases: tuple[EvaluationCase, ...]

    @property
    def passed(self) -> bool:
        return all(case.passed for case in self.cases)

    def to_dict(self) -> dict[str, Any]:
        passed_count = sum(case.passed for case in self.cases)
        return {
            "schema_version": "1.0",
            "service": "kite-x402-fastapi",
            "network": self.network,
            "asset": self.asset,
            "price": self.price,
            "summary": {
                "status": "passed" if self.passed else "failed",
                "total": len(self.cases),
                "passed": passed_count,
                "failed": len(self.cases) - passed_count,
            },
            "cases": [case.to_dict() for case in self.cases],
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    def to_text(self) -> str:
        summary = self.to_dict()["summary"]
        lines = [
            f"x402 service evaluation: {summary['status'].upper()} "
            f"({summary['passed']}/{summary['total']} passed)",
            f"Network: {self.network} | asset: {self.asset} | price: {self.price}",
        ]
        for case in self.cases:
            marker = "PASS" if case.passed else "FAIL"
            lines.append(f"{marker} {case.id}: {case.name}")
            for check in case.checks:
                if not check.passed:
                    lines.append(f"  - {check.name}: expected {check.expected!r}, got {check.actual!r}")
            if case.error:
                lines.append(f"  - error: {case.error}")
        return "\n".join(lines)


ServiceFactory = Callable[..., FastAPI]


def evaluate_service(
    *,
    app_factory: ServiceFactory = create_app,
    settings: Settings | None = None,
) -> EvaluationReport:
    """Evaluate a FastAPI x402 app using the project's injectable-client contract.

    ``app_factory`` receives ``settings``, ``facilitator``, and
    ``upstream_client`` keyword arguments, matching :func:`create_app`.
    """
    settings = settings or Settings(pay_to=PAY_TO, upstream_url=DEFAULT_UPSTREAM_URL)
    chain = kite_chain_by_name(settings.kite_network)
    cases: list[EvaluationCase] = []

    def record(case_id: str, name: str, run: Callable[[], list[Check]]) -> None:
        try:
            checks = tuple(run())
            cases.append(EvaluationCase(case_id, name, checks))
        except Exception as error:  # Keep the report useful when one scenario crashes.
            cases.append(EvaluationCase(case_id, name, (), error=f"{type(error).__name__}: {error}"))

    def check(name: str, actual: Any, expected: Any) -> Check:
        return Check(name=name, passed=actual == expected, expected=expected, actual=actual)

    def no_payment() -> list[Check]:
        mock = MockNetwork(chain)
        with running(network=mock, settings=settings, app_factory=app_factory) as (client, network):
            response = client.get("/v1/hello?name=Kite")
            challenge = decode(response.headers["payment-required"]) if "payment-required" in response.headers else {}
            accepts = challenge.get("accepts", [])
            required = accepts[0] if accepts else {}
            expected_accept = {
                "scheme": "exact", "network": chain.network, "asset": chain.asset_address,
                "amount": chain.money_parser(settings.price, chain.network).amount,
                "payTo": settings.pay_to, "maxTimeoutSeconds": 60,
                "extra": {"name": chain.eip712_name, "version": chain.eip712_version},
            }
            challenge_valid = (
                challenge.get("x402Version") == 2
                and required == expected_accept
                and challenge.get("resource", {}).get("mimeType") == "application/json"
                and challenge.get("resource", {}).get("url") == "http://testserver/v1/hello?name=Kite"
            )
            return [
                check("HTTP status", response.status_code, 402),
                check("payment requirements are valid", challenge_valid, True),
                check("facilitator operations", network.events, []),
                check("upstream calls", len(network.upstream_requests), 0),
                check("settlement attempts", network.events.count("settle"), 0),
            ]

    def invalid_payment() -> list[Check]:
        mock = MockNetwork(chain)
        mock.verify_valid = False
        with running(network=mock, settings=settings, app_factory=app_factory) as (client, network):
            response = client.get("/v1/hello", headers=payment_header(client))
            return [
                check("verification rejection status", response.status_code, 402),
                check("payment failure is reported", "payment-required" in response.headers, True),
                check("facilitator operations", network.events, ["verify"]),
                check("upstream calls", len(network.upstream_requests), 0),
                check("settlement attempts", network.events.count("settle"), 0),
            ]

    def valid_payment() -> list[Check]:
        mock = MockNetwork(chain)
        with running(network=mock, settings=settings, app_factory=app_factory) as (client, network):
            response = client.get("/v1/hello", headers=payment_header(client))
            settled = decode(response.headers["payment-response"]) if "payment-response" in response.headers else {}
            return [
                check("HTTP status", response.status_code, 200),
                check("response body", response.content, mock.upstream_body),
                check("operation order", network.events, ["verify", "upstream", "settle"]),
                check("settlement succeeded", settled.get("success"), True),
            ]

    def replay_protection() -> list[Check]:
        mock = MockNetwork(chain)
        with running(network=mock, settings=settings, app_factory=app_factory) as (client, network):
            credentials = payment_header(client)
            first = client.get("/v1/hello", headers=credentials)
            second = client.get("/v1/hello", headers=credentials)
            first_settlement = (
                decode(first.headers["payment-response"])
                if "payment-response" in first.headers else {}
            )
            return [
                check("first payment succeeds", first.status_code, 200),
                check("first settlement succeeds", first_settlement.get("success"), True),
                check("replayed payment is rejected", second.status_code, 402),
                check("replay is identified", second.headers.get("x-payment-replay"), "true"),
                check("settlement attempts", network.events.count("settle"), 1),
                check("upstream calls", len(network.upstream_requests), 1),
                check("facilitator operations", network.events, ["verify", "upstream", "settle"]),
            ]

    def upstream_failure() -> list[Check]:
        mock = MockNetwork(chain)
        mock.upstream_status = 503
        with running(network=mock, settings=settings, app_factory=app_factory) as (client, network):
            response = client.get("/v1/hello", headers=payment_header(client))
            return [
                check("upstream error is propagated", response.status_code, 503),
                check("upstream body is propagated", response.content, mock.upstream_body),
                check("operation order", network.events, ["verify", "upstream"]),
                check("settlement attempts", network.events.count("settle"), 0),
            ]

    def mismatched_payment(field: str, value: Any) -> list[Check]:
        mock = MockNetwork(chain)

        def mutate(payload: dict[str, Any]) -> None:
            payload["accepted"][field] = value

        with running(network=mock, settings=settings, app_factory=app_factory) as (client, network):
            response = client.get("/v1/hello", headers=payment_header(client, mutate=mutate))
            return [
                check("mismatched payment is rejected", response.status_code, 402),
                check("facilitator operations", network.events, []),
                check("upstream calls", len(network.upstream_requests), 0),
                check("settlement attempts", network.events.count("settle"), 0),
            ]

    def settlement_failure() -> list[Check]:
        mock = MockNetwork(chain)
        mock.settle_success = False
        with running(network=mock, settings=settings, app_factory=app_factory) as (client, network):
            response = client.get("/v1/hello", headers=payment_header(client))
            settled = decode(response.headers["payment-response"]) if "payment-response" in response.headers else {}
            return [
                check("settlement failure status", response.status_code, 402),
                check("upstream body is withheld", response.content, b"{}"),
                check("settlement failure is reported", settled.get("success"), False),
                check("operation order", network.events, ["verify", "upstream", "settle"]),
            ]

    def upstream_unreachable() -> list[Check]:
        mock = MockNetwork(chain)
        mock.transport_error_at = "upstream"
        with running(network=mock, settings=settings, app_factory=app_factory) as (client, network):
            response = client.get("/v1/hello", headers=payment_header(client))
            body = response.json()
            return [
                check("unreachable upstream status", response.status_code, 502),
                check("unreachable upstream error", body.get("error"), "upstream unreachable"),
                check("operation order", network.events, ["verify", "upstream"]),
                check("settlement attempts", network.events.count("settle"), 0),
            ]

    record("no_payment", "Rejects unpaid requests with valid requirements", no_payment)
    record("invalid_payment", "Fails closed on unverifiable payment", invalid_payment)
    record("valid_payment", "Verifies, calls upstream, then settles", valid_payment)
    record("replay_protection", "Rejects a repeated payment payload after one settlement", replay_protection)
    record("upstream_failure", "Propagates upstream errors without settlement", upstream_failure)
    record("wrong_network", "Rejects a payment for another network", lambda: mismatched_payment("network", "eip155:1"))
    record("wrong_asset", "Rejects a payment for another asset", lambda: mismatched_payment("asset", "0x" + "33" * 20))
    record(
        "incompatible_domain",
        "Rejects incompatible EIP-712 token metadata",
        lambda: mismatched_payment("extra", {"name": "Wrong Token", "version": "999"}),
    )
    record("settlement_failure", "Withholds upstream response when settlement fails", settlement_failure)
    record("upstream_unreachable", "Does not settle when the upstream cannot be reached", upstream_unreachable)

    return EvaluationReport(chain.network, chain.asset_symbol, settings.price, tuple(cases))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run deterministic x402 service conformance checks.")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="output format (default: text)")
    parser.add_argument("--network", choices=("mainnet", "testnet"), default="mainnet", help="Kite challenge network")
    parser.add_argument("--pay-to", default=PAY_TO, help="pay-to address used in the generated challenge")
    parser.add_argument("--price-usd", default="0.001", help="price used in the generated challenge")
    args = parser.parse_args()

    settings = Settings(
        pay_to=args.pay_to,
        upstream_url=DEFAULT_UPSTREAM_URL,
        kite_network=args.network,
        price_usd=args.price_usd,
    )
    report = evaluate_service(settings=settings)
    print(report.to_json() if args.format == "json" else report.to_text())
    raise SystemExit(0 if report.passed else 1)


if __name__ == "__main__":
    main()
