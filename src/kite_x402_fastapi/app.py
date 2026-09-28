"""Official x402 middleware plus a small HTTP reverse proxy."""

import asyncio
import base64
import hashlib
import json
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from x402 import x402ResourceServer
from x402.http import FacilitatorConfig, HTTPFacilitatorClient
from x402.http.facilitator_client_base import FacilitatorClient
from x402.http.middleware.fastapi import payment_middleware
from x402.http.types import PaymentOption, RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme

from .config import Settings
from .kite import kite_chain_by_name

HOP_BY_HOP = {
    "connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
    "host", "content-length", "proxy-authenticate", "proxy-authorization",
}


class _ReplayGuard:
    """Process-local protection against resubmitting a settled payload."""

    def __init__(self) -> None:
        self._in_flight: set[str] = set()
        self._settled: set[str] = set()
        self._lock = asyncio.Lock()

    async def claim(self, fingerprint: str) -> bool:
        async with self._lock:
            if fingerprint in self._in_flight or fingerprint in self._settled:
                return False
            self._in_flight.add(fingerprint)
            return True

    async def finish(self, fingerprint: str, settled: bool) -> None:
        async with self._lock:
            self._in_flight.discard(fingerprint)
            if settled:
                self._settled.add(fingerprint)


def _settlement_succeeded(response: Response | None) -> bool:
    if response is None:
        return False
    header = response.headers.get("payment-response")
    if not header:
        return False
    try:
        result = json.loads(base64.b64decode(header))
    except (ValueError, json.JSONDecodeError):
        return False
    return isinstance(result, dict) and result.get("success") is True


def _payment_fingerprint(payment: str) -> str:
    try:
        payload = json.loads(base64.b64decode(payment, validate=True))
        if isinstance(payload, dict):
            payment_data = payload.get("payload")
            authorization = payment_data.get("authorization") if isinstance(payment_data, dict) else None
            accepted = payload.get("accepted")
            if isinstance(accepted, dict) and isinstance(authorization, dict):
                # Resource URLs and JSON formatting do not distinguish an already-used authorization.
                payload = {"accepted": accepted, "authorization": authorization}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (ValueError, json.JSONDecodeError):
        canonical = payment.encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def create_app(
    settings: Settings | None = None,
    *,
    facilitator: FacilitatorClient | None = None,
    upstream_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Create the wrapper. Injected clients remain owned by the caller."""
    settings = settings or Settings.from_env()
    chain = kite_chain_by_name(settings.kite_network)
    owned_facilitator = facilitator is None
    facilitator = facilitator or HTTPFacilitatorClient(
        FacilitatorConfig(url=settings.facilitator_url, timeout=30)
    )
    server = x402ResourceServer(facilitator).register(
        chain.network, ExactEvmServerScheme().register_money_parser(chain.money_parser)
    )
    routes = {
        "/v1/*": RouteConfig(
            accepts=PaymentOption(
                scheme="exact", pay_to=settings.pay_to, price=settings.price,
                network=chain.network, max_timeout_seconds=60,
            ),
            description=settings.service_description,
            mime_type="application/json",
        )
    }
    payments = payment_middleware(routes, server)
    replay_guard = _ReplayGuard()

    async def protected_payments(request: Request, call_next):
        payment = request.headers.get("payment-signature") or request.headers.get("x-payment")
        if not payment or not request.scope["path"].startswith("/v1/"):
            return await payments(request, call_next)

        fingerprint = _payment_fingerprint(payment)
        if not await replay_guard.claim(fingerprint):
            return JSONResponse(
                {"error": "payment payload has already been used"},
                status_code=402,
                headers={"X-Payment-Replay": "true", "Cache-Control": "no-store"},
            )

        response = None
        try:
            response = await payments(request, call_next)
            return response
        finally:
            await replay_guard.finish(fingerprint, _settlement_succeeded(response))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = upstream_client or httpx.AsyncClient(timeout=60, follow_redirects=True)
        app.state.upstream_client = client
        try:
            yield
        finally:
            if upstream_client is None:
                await client.aclose()
            if owned_facilitator:
                await facilitator.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.middleware("http")(protected_payments)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "network": chain.network, "asset": chain.asset_symbol, "price": settings.price}

    @app.api_route(
        "/v1/{path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"],
    )
    async def proxy(request: Request):
        # Keep the configured origin fixed and preserve escaped paths and query bytes.
        raw_path = request.scope["raw_path"].split(b"?", 1)[0][len(b"/v1"):]
        target = httpx.URL(settings.upstream_url).copy_with(
            raw_path=raw_path + (b"?" + request.scope["query_string"] if request.scope["query_string"] else b"")
        )
        blocked = HOP_BY_HOP | {"payment-signature"}
        blocked |= {part.strip().lower() for part in request.headers.get("connection", "").split(",")}
        headers = {k: v for k, v in request.headers.items() if k.lower() not in blocked}
        if settings.upstream_auth_value:
            headers[settings.upstream_auth_header.lower()] = settings.upstream_auth_value
        body = await request.body() if request.method not in ("GET", "HEAD") else None
        try:
            result = await request.app.state.upstream_client.request(
                request.method, target, headers=headers, content=body, follow_redirects=True,
            )
        except httpx.HTTPError as error:
            return JSONResponse({"error": "upstream unreachable", "detail": str(error)}, status_code=502)
        blocked = HOP_BY_HOP | {"content-encoding"}
        blocked |= {part.strip().lower() for part in result.headers.get("connection", "").split(",")}
        return Response(
            content=result.content,
            status_code=result.status_code,
            headers={k: v for k, v in result.headers.items() if k.lower() not in blocked},
        )

    return app
