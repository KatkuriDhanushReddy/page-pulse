# Page Pulse

Production-grade URL audit service.

**Built for [Digital Heroes Training Task](https://digitalheroesco.com)**

| | |
|---|---|
| Public repo | https://github.com/KatkuriDhanushReddy/page-pulse |
| Live demo | https://page-pulse-u40v.onrender.com |
| API docs (Swagger) | https://page-pulse-u40v.onrender.com/docs |
| Health | https://page-pulse-u40v.onrender.com/api/health |

---

## What it does

`POST /api/audit` fetches a public URL and returns a structured report:

- HTTP status and accessibility
- Response latency
- TLS certificate validity / expiry
- Page metadata (title, description, Open Graph, H1 count)
- Selected response headers

Built for production, not a demo:

| Requirement | Implementation |
|---|---|
| Input validation | Pydantic models; only absolute `http`/`https` URLs accepted |
| Request timeouts | Configurable; default **10 s** per outbound fetch |
| Concurrency limits | `asyncio.Semaphore`; default **50** in-flight audits per process |
| Structured errors | Every non-2xx body is `{ "error": { code, message, request_id, details } }` |
| Caching | SHA-256(URL) key, configurable TTL (default **300 s**) |
| Rate limiting | Per-client fixed window (default **100 / hour**) |
| Structured logging | One JSON line per event, every line carries `request_id` |
| SSRF guard | Private / loopback / link-local targets refused by default |

---

## Quick start

```bash
# 1. Python 3.11+
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# 2. Install
pip install -r requirements-dev.txt

# 3. Run (in-memory cache + rate limiter — no database required)
uvicorn app.main:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000) for the console,
[http://localhost:8000/docs](http://localhost:8000/docs) for the interactive contract.

### Optional: shared MongoDB backend

```bash
copy .env.example .env   # Windows
# set MONGO_URL=mongodb://localhost:27017
uvicorn app.main:app --reload --port 8000
```

When `MONGO_URL` is empty the service uses process-local stores (correct for a
single instance and for CI). When set, cache and rate-limit counters are shared
across replicas.

---

## API contract

Base path: `/api`

All responses include `X-Request-ID`. Audit endpoints also return:

| Header | Meaning |
|---|---|
| `X-RateLimit-Limit` | Window ceiling for this client |
| `X-RateLimit-Remaining` | Requests left in the current window |
| `X-RateLimit-Reset` | Seconds until the window rolls over |
| `X-Cache` | `HIT` or `MISS` (single-audit only) |

Client identity, in order of preference: `X-Client-ID` header → `client_id` in
the body → peer IP / `X-Forwarded-For`.

### `GET /api/health`

Liveness. Does not touch storage.

```http
GET /api/health
```

```json
{
  "status": "ok",
  "service": "page-pulse",
  "version": "1.0.0",
  "storage": "memory",
  "uptime_seconds": 42.1,
  "checked_at": "2026-07-25T14:30:00.123456+00:00"
}
```

### `GET /api/ready`

Readiness. Probes the cache store. Returns `503` if storage is unreachable.

### `POST /api/audit`

Audit one URL.

```http
POST /api/audit
Content-Type: application/json
X-Client-ID: demo-client
X-Request-ID: optional-caller-trace-id

{
  "url": "https://example.com",
  "client_id": "demo-client"
}
```

**200 — success (or graceful upstream failure):**

```json
{
  "request_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "url": "https://example.com",
  "final_url": "https://example.com/",
  "status_code": 200,
  "accessible": true,
  "response_time_ms": 187.4,
  "ssl": {
    "valid": true,
    "issuer": "Let's Encrypt",
    "subject": "example.com",
    "expires_at": "2026-12-15T23:59:59+00:00",
    "days_remaining": 143,
    "error": null
  },
  "meta": {
    "title": "Example Domain",
    "description": "This domain is for use in documentation examples.",
    "canonical": "https://example.com/",
    "robots": null,
    "og_title": null,
    "og_description": null,
    "h1_count": 1
  },
  "performance": {
    "total_time_ms": 187.4,
    "ttfb_ms": 160.2,
    "content_bytes": 1256,
    "redirect_count": 0,
    "truncated": false
  },
  "headers": {
    "content-type": "text/html; charset=UTF-8",
    "server": "nginx"
  },
  "error": null,
  "cached": false,
  "checked_at": "2026-07-25T14:30:00.123456+00:00"
}
```

A target that times out or refuses the connection still returns **HTTP 200**
with `accessible: false` and a populated `error` object. That is deliberate:
the *audit completed*; the *target* failed. Transport / validation failures of
*our* API use 4xx/5xx.

**422 — validation:**

```json
{
  "error": {
    "code": "validation_error",
    "message": "Request payload failed validation.",
    "request_id": "…",
    "details": { "errors": [ /* pydantic errors */ ] }
  }
}
```

**429 — rate limited:**

```json
{
  "error": {
    "code": "rate_limit_exceeded",
    "message": "Client exceeded 100 requests per window.",
    "request_id": "…",
    "details": { "limit": 100, "reset_in_seconds": 2415 }
  }
}
```

Headers on 429 include `Retry-After`.

### `POST /api/audit/batch`

Audit up to `MAX_BATCH_SIZE` URLs (default 10). Costs one rate-limit unit per URL.

```http
POST /api/audit/batch
Content-Type: application/json
X-Client-ID: demo-client

{
  "urls": [
    { "url": "https://example.com" },
    { "url": "https://example.org" }
  ]
}
```

```json
{
  "request_id": "…",
  "count": 2,
  "results": [ /* AuditResult, … */ ]
}
```

### `POST /api/cache/purge`

Removes expired cache entries. Useful for ops; not required for correctness
(lookups already treat expired entries as misses).

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `MONGO_URL` | _(empty)_ | Shared store; empty → in-memory |
| `DB_NAME` | `page_pulse` | Mongo database name |
| `CACHE_TTL_SECONDS` | `300` | Cache window |
| `RATE_LIMIT_REQUESTS` | `100` | Requests per window per client |
| `RATE_LIMIT_WINDOW_SECONDS` | `3600` | Rate-limit window |
| `AUDIT_TIMEOUT_SECONDS` | `10` | Outbound request timeout |
| `AUDIT_MAX_CONCURRENCY` | `50` | In-flight audits per process |
| `AUDIT_MAX_DOWNLOAD_BYTES` | `2000000` | Body size cap |
| `AUDIT_MAX_REDIRECTS` | `5` | Redirect follow limit |
| `MAX_BATCH_SIZE` | `10` | Batch endpoint ceiling |
| `ALLOW_PRIVATE_TARGETS` | `false` | SSRF guard (set `true` only for local tests) |
| `CORS_ORIGINS` | `*` | Comma-separated origins |
| `LOG_LEVEL` | `INFO` | Root log level |

See [`.env.example`](.env.example).

---

## Tests & CI

```bash
pytest --cov=app --cov-report=term-missing
```

The suite covers:

- URL validation (accept / reject matrix)
- Successful audits, meta extraction, redirects, truncation
- Timeout / connection / redirect-loop structured errors
- Concurrency semaphore ceiling
- Cache hit / miss / TTL / no-cache-on-failure
- Per-client rate limiting under concurrency
- Full ASGI path: headers, 422, 429, batch costing

CI (`.github/workflows/ci.yml`) runs on **every push**:

1. `ruff` lint + format check
2. `pytest` with coverage gate (≥ 80%) on Python 3.11 and 3.12
3. Docker build + smoke test against `/api/health` and validation 422

No external network and no database are required for the unit/API tests —
outbound HTTP is stubbed with `httpx.MockTransport`.

---

## Deploy

### Docker

```bash
docker build -t page-pulse .
docker run --rm -p 8000:8000 page-pulse
```

### Suggested free hosts

| Host | Notes |
|---|---|
| [Render](https://render.com) | Web service from this repo; set env vars from the table above |
| [Railway](https://railway.app) | Same; add Mongo plugin if you want shared state |
| [Fly.io](https://fly.io) | `fly launch` from the Dockerfile |

After deploy:

1. Confirm the footer on `/` reads **Built for Digital Heroes Training Task** and links to `https://digitalheroesco.com`.
2. Paste the live URL into the table at the top of this README.
3. Confirm GitHub → Settings → Pages/Actions shows a green CI run on `main`.

---

## Design docs (Task B)

| Document | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Components, data flow, queueing, state, TDRs, three failure modes |
| [docs/observability.md](docs/observability.md) | Metrics, alerts, deploy strategy, rollback |

---

## Project layout

```
page-pulse/
├── app/
│   ├── main.py            # FastAPI app, middleware, routes
│   ├── auditor.py         # Outbound fetch + TLS + meta extraction
│   ├── storage.py         # Memory + Mongo cache / rate-limit backends
│   ├── models.py          # Request / response contract
│   ├── config.py          # Env-driven settings
│   └── logging_setup.py   # JSON logs + request-id context
├── static/index.html      # Live console (credit line in footer)
├── tests/                 # pytest suite
├── docs/                  # Task B deliverables
├── .github/workflows/ci.yml
├── Dockerfile
└── README.md              # this file
```

---

## Assumptions

1. Targets are public internet URLs. Private ranges are refused unless
   `ALLOW_PRIVATE_TARGETS=true`.
2. A single-instance deploy with in-memory stores is valid for the live demo;
   multi-replica production should set `MONGO_URL` (or swap in Redis — see
   architecture TDR §4.2).
3. Audit results are not tenant-secret, so the cache key is the URL alone.
4. "Accessible" means HTTP status ∈ [200, 400). A 404 is a successful audit of
   an inaccessible page, not an API error.

---

## AI use (required disclosure)

AI assisted scaffolding, boilerplate, and first drafts of the architecture /
observability docs. Every production decision (timeouts, semaphore sizing,
fail-open rate limiting, "don't cache failures", SSRF guard, batch costing,
structured error shape) was reviewed and owned by me. Tests were written to
lock those decisions in, not to rubber-stamp generated code. See
[docs/ai-usage.md](docs/ai-usage.md) for the full note.

---

**Built for [Digital Heroes Training Task](https://digitalheroesco.com)**
