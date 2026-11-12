import json
import logging
import time
from contextlib import asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .extract import session_key
from .policy import CONTEXT_EDITING_BETA, Decision, Router, rewrite
from .pricing import Pricing
from .tracker import Tracker, start_of_day

log = logging.getLogger("laya_tier.proxy")

REQUEST_DROP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "accept-encoding",
                "upgrade", "proxy-connection", "te", "trailer"}
RESPONSE_DROP = {"content-length", "content-encoding", "transfer-encoding", "connection", "keep-alive"}
RETRY_STATUSES = (400, 404, 422)
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


def forward_headers(request, extra_beta=None):
    headers = [(k, v) for k, v in request.headers.items() if k.lower() not in REQUEST_DROP]
    headers.append(("accept-encoding", "identity"))
    if extra_beta:
        betas = [b.strip() for k, v in headers if k.lower() == "anthropic-beta" for b in v.split(",") if b.strip()]
        headers = [(k, v) for k, v in headers if k.lower() != "anthropic-beta"]
        if extra_beta not in betas:
            betas.append(extra_beta)
        headers.append(("anthropic-beta", ",".join(betas)))
    return headers


def response_headers(resp, extra=None):
    headers = {k: v for k, v in resp.headers.items() if k.lower() not in RESPONSE_DROP}
    headers.update(extra or {})
    return headers


def upstream_url(request):
    return request.url.path + ("?" + request.url.query if request.url.query else "")


def error_body(kind, message):
    return {"type": "error", "error": {"type": kind, "message": message}}


class SSEUsage:
    def __init__(self):
        self.buffer = b""
        self.usage = {}

    def feed(self, chunk):
        self.buffer += chunk
        while True:
            self.buffer = self.buffer.replace(b"\r\n", b"\n")
            end = self.buffer.find(b"\n\n")
            if end < 0:
                return
            event, self.buffer = self.buffer[:end], self.buffer[end + 2:]
            for line in event.split(b"\n"):
                if line.startswith(b"data:"):
                    try:
                        self.apply(json.loads(line[5:].strip()))
                    except ValueError:
                        pass

    def apply(self, data):
        if not isinstance(data, dict):
            return
        if data.get("type") == "message_start":
            self.merge((data.get("message") or {}).get("usage"))
        elif data.get("type") == "message_delta":
            self.merge(data.get("usage"))

    def merge(self, usage):
        if isinstance(usage, dict):
            for key, value in usage.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    self.usage[key] = value
                elif key == "cache_creation" and isinstance(value, dict):
                    self.usage[key] = dict(self.usage.get(key) or {}, **value)


def create_app(config, classifier=None, transport=None, tracker=None):
    pricing = Pricing(config.prices)
    owns_tracker = tracker is None
    tracker = tracker or Tracker(config.db_path)
    client = httpx.AsyncClient(base_url=config.upstream, transport=transport,
                               timeout=httpx.Timeout(600.0, connect=15.0))
    state = {"router": Router(config, classifier, pricing) if classifier is not None else None}

    @asynccontextmanager
    async def lifespan(app):
        if state["router"] is None:
            from laya_tier import TierClassifier

            log.warning("loading classifier from %s", config.classifier_model)
            loaded = await run_in_threadpool(TierClassifier.load, config.classifier_model,
                                             config.classifier_device, config.min_confidence)
            await run_in_threadpool(loaded.classify, "warm up")
            state["router"] = Router(config, loaded, pricing)
            log.warning("laya-tier proxy ready on http://%s:%s -> %s%s", config.host, config.port, config.upstream,
                        " (shadow mode)" if config.shadow else "")
        yield
        await client.aclose()
        if owns_tracker:
            tracker.close()

    def over_budget(session):
        budget, over = config.budget, []
        if budget.daily_usd is not None and tracker.spend(since=start_of_day()) >= budget.daily_usd:
            over.append("daily")
        if budget.session_usd is not None and tracker.spend(session=session) >= budget.session_usd:
            over.append("session")
        return over

    async def send(method, url, content, headers):
        request = client.build_request(method, url, content=content, headers=headers)
        return await client.send(request, stream=True)

    def finish(decision, body, sent_model, applied, retried, status, usage, started, stream):
        cost = pricing.cost(sent_model, usage)
        if applied:
            counterfactual = pricing.cost(decision.requested_model, usage)
        elif config.shadow and decision.switches:
            counterfactual = pricing.cost(decision.target_model, usage)
        else:
            counterfactual = cost
        tracker.record(session=decision.session, role=decision.role, thread=decision.thread,
                       tier=decision.tier, confidence=decision.confidence,
                       source=decision.source, reason=decision.reason, requested_model=decision.requested_model,
                       target_model=decision.target_model, sent_model=sent_model, effort=decision.effort,
                       applied=int(applied), shadow=int(config.shadow), retried=int(retried), stream=int(stream),
                       status=status, input_tokens=int(usage.get("input_tokens") or 0),
                       cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
                       cache_write_1h_tokens=int((usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens")
                                                 or 0),
                       cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
                       output_tokens=int(usage.get("output_tokens") or 0), cost_usd=cost,
                       counterfactual_usd=counterfactual, latency_ms=(time.perf_counter() - started) * 1000,
                       classify_ms=decision.classify_ms, prompt_hash=decision.prompt_hash, prompt=decision.prompt)
        log.info("%s/%s %-7s %s -> %s%s status=%s in=%s out=%s cost=%s", decision.role, decision.source,
                 decision.tier or "-",
                 decision.requested_model, sent_model, " (retried original)" if retried else "", status,
                 usage.get("input_tokens"), usage.get("output_tokens"),
                 "%.5f" % cost if cost is not None else "?")

    async def passthrough(request):
        raw = await request.body()
        try:
            resp = await send(request.method, upstream_url(request), raw, forward_headers(request))
        except httpx.HTTPError as exc:
            return JSONResponse(error_body("api_error", "laya-tier upstream error: %s" % exc), status_code=502)

        async def body():
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()

        return StreamingResponse(body(), status_code=resp.status_code, headers=response_headers(resp))

    async def messages(request):
        raw = await request.body()
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if not isinstance(body, dict) or state["router"] is None:
            return await passthrough(request)
        started = time.perf_counter()
        session = session_key(body)
        over = over_budget(session) if (config.budget.daily_usd or config.budget.session_usd) else []
        if over and config.budget.action == "block":
            decision = Decision(session=session, requested_model=body.get("model"), reason="budget_" + "+".join(over))
            finish(decision, body, None, False, False, 429, {}, started, bool(body.get("stream")))
            return JSONResponse(error_body("rate_limit_error", "laya-tier budget exceeded (%s)" % ", ".join(over)),
                                status_code=429)
        force = "simple" if over and config.budget.action == "downgrade" else None
        try:
            decision = await run_in_threadpool(state["router"].decide, body, force)
        except Exception as exc:
            log.exception("routing failed, passing request through")
            decision = Decision(session=session, requested_model=body.get("model"),
                                target_model=body.get("model"), reason="router_error:%s" % type(exc).__name__)
        if over:
            decision.reason = (decision.reason + " budget_" + "+".join(over)).strip()
        new_body, changed = rewrite(body, decision, config.shadow)
        beta = CONTEXT_EDITING_BETA if decision.context_editing and changed else None
        url = upstream_url(request)
        retried = False
        try:
            resp = await send("POST", url, json.dumps(new_body).encode() if changed else raw,
                              forward_headers(request, beta))
            if changed and config.retry_original_on_error and resp.status_code in RETRY_STATUSES:
                await resp.aread()
                log.warning("upstream rejected rewritten request (%s): %s", resp.status_code, resp.text[:300])
                await resp.aclose()
                resp = await send("POST", url, raw, forward_headers(request))
                retried = True
        except httpx.HTTPError as exc:
            finish(decision, body, body.get("model") if retried else new_body.get("model"), False, retried, 502, {},
                   started, bool(body.get("stream")))
            return JSONResponse(error_body("api_error", "laya-tier upstream error: %s" % exc), status_code=502)
        applied = changed and decision.switches and not retried
        sent_model = new_body.get("model") if changed and not retried else body.get("model")
        extra = {"x-laya-tier": decision.tier or "-", "x-laya-tier-model": sent_model or "-",
                 "x-laya-tier-source": decision.source, "x-laya-tier-role": decision.role}
        headers = response_headers(resp, extra)
        if resp.headers.get("content-type", "").startswith("text/event-stream"):
            parser = SSEUsage()

            async def stream():
                try:
                    async for chunk in resp.aiter_bytes():
                        parser.feed(chunk)
                        yield chunk
                finally:
                    await resp.aclose()
                    finish(decision, body, sent_model, applied, retried, resp.status_code, parser.usage, started, True)

            return StreamingResponse(stream(), status_code=resp.status_code, headers=headers)
        data = await resp.aread()
        await resp.aclose()
        try:
            usage = (json.loads(data) or {}).get("usage") or {}
        except (ValueError, AttributeError):
            usage = {}
        finish(decision, body, sent_model, applied, retried, resp.status_code, usage, started, False)
        return Response(data, status_code=resp.status_code, headers=headers)

    routes = [Route("/v1/messages", messages, methods=["POST"]),
              Route("/{path:path}", passthrough, methods=METHODS)]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.tracker = tracker
    app.state.config = config
    app.state.shared = state
    return app
