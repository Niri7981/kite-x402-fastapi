"""Official x402 middleware plus a small HTTP reverse proxy."""

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
    app.middleware("http")(payments)

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
