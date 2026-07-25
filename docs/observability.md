# Observability and rollback plan

Companion to [architecture.md](architecture.md). Covers what to watch, when to wake
someone up, and how to undo a bad release without guessing.

---

## 1. Metrics that earn their keep

If a metric does not drive a decision, it is noise. These drive decisions.

### Golden signals (request path)

| Metric | Type | Labels | Why it matters |
|---|---|---|---|
| `http_requests_total` | counter | `method`, `route`, `status` | Traffic shape and error budget |
| `http_request_duration_seconds` | histogram | `route` | Customer-facing SLA (`p50` / `p95` / `p99`) |
| `audit_upstream_duration_seconds` | histogram | `outcome` | Separates *our* latency from the target's |
| `audit_errors_total` | counter | `code` | `upstream_timeout`, `connection_failed`, `target_not_allowed`, … |
| `cache_requests_total` | counter | `result=hit\|miss` | Cache effectiveness |
| `ratelimit_decisions_total` | counter | `allowed` | Abuse vs under-provisioned limits |
| `audit_inflight` | gauge | — | Semaphore occupancy (burst indicator) |
| `audit_queue_wait_seconds` | histogram | — | Time spent waiting for a semaphore slot |

### Dependency signals

| Metric | Alert when | Meaning |
|---|---|---|
| `store_operation_duration_seconds` | p95 > 250 ms | Shared cache/rate-limit store is the bottleneck |
| `store_errors_total` | rate > 1%/min | Store failing open/closed as designed — still needs eyes |
| `store_pool_in_use` | > 80% of max | Approaching connection exhaustion |
| `process_resident_memory_bytes` | trending up across deploys | Leak or body-cap misconfig |

### How to export them

Prometheus `/metrics` via `prometheus-client` (or OpenTelemetry → collector).
Every log line is already JSON with `request_id` — that is the join key between
metrics and traces when investigating a single slow call.

---

## 2. Alerts

Two severities only. Everything else is a dashboard.

### Page (wake the on-call)

| Alert | Condition | Why it pages |
|---|---|---|
| **Error budget burn** | 5xx rate > 5% for 5 minutes *or* structured `error.code` rate > 10% for 5 minutes | Customers are failing now |
| **SLA breach** | `http_request_duration_seconds` p95 > 3 s for 10 minutes | The customer-facing promise is broken |
| **Service down** | `/api/health` fails 3 consecutive probes from two zones | Nothing else matters |
| **Crash loop** | Pod restart count > 3 in 10 minutes | Deploy or OOM — either needs a human |

### Notify (Slack, no wake-up)

| Alert | Condition | Likely cause |
|---|---|---|
| Cache hit rate collapse | Hit rate drops >20 pts vs 7-day baseline for 15 min | Stampede, TTL misconfig, unique-URL flood |
| Upstream timeout spike | `upstream_timeout` > 5% for 10 min | One pathological host (see failure mode 1) |
| Rate-limit saturation | Blocked decisions > 100/hour for one client | Abuse or a legitimate client that needs a higher quota |
| Store degraded | `store_errors_total` > 1%/min for 5 min | Failover / pool pressure — service is still up |
| Queue depth | `audit_inflight` at ceiling for > 2 min | Burst — HPA should already be scaling |

Every page alert has a runbook link in the annotation. No alert without an owner
and a first action.

---

## 3. Dashboards (one screen each)

1. **Service overview** — RPS, error %, p50/p95/p99, cache hit rate, inflight.
2. **Upstream health** — timeout rate by host, redirect counts, truncated bodies.
3. **Deploy health** — error rate and latency annotated with deploy markers (the
   chart that makes rollback a 30-second decision).

---

## 4. Logging contract

Every request emits exactly one access line:

```json
{
  "ts": "2026-07-25T14:30:00.123Z",
  "level": "INFO",
  "logger": "page_pulse",
  "message": "request.completed",
  "request_id": "a1b2c3d4-…",
  "method": "POST",
  "path": "/api/audit",
  "status": 200,
  "duration_ms": 187.4
}
```

Audit outcomes emit a second line (`audit.completed` / `audit.failed`) with
`url`, `status_code` or `error_code`, and `duration_ms`. Retention: 30 days for
INFO, 90 days for WARNING+.

PII note: URLs can contain tokens in query strings. Redact known secret query
params (`token`, `key`, `password`, `session`) before logging.

---

## 5. Deployment strategy

**Default: rolling update with a readiness gate.**

1. New pods start but receive no traffic until `/api/ready` returns 200
   (proves the store answers, not just that the process is alive).
2. MaxUnavailable = 0, MaxSurge = 25% — capacity never dips during a deploy.
3. Automated smoke after each surge:
   - `GET /api/health` → 200
   - `POST /api/audit` with a known-good URL → 200, `accessible: true`
   - `POST /api/audit` with `not-a-url` → 422 structured error
4. Deploy is marked healthy only after smoke + 5 minutes of error rate within
   2× the previous baseline.

**Blue/green** is reserved for schema-changing store migrations. For this
service's day-to-day deploys, rolling is simpler and equivalent because pods
are stateless.

---

## 6. Rollback

### Automatic triggers (within 10 minutes of a deploy)

- Error rate > 10% for 2 minutes
- Health/ready probe failing for 3 consecutive checks
- Crash loop (restarts > 3 in 5 minutes)
- p99 latency > 10 s for 2 minutes

### Manual decision

Any SEV-2 customer report, data corruption suspicion, or security finding.

### Execution (Kubernetes)

```bash
# See what is live and what came before
kubectl rollout history deployment/page-pulse

# Undo the last deploy
kubectl rollout undo deployment/page-pulse

# Or pin a known-good revision
kubectl rollout undo deployment/page-pulse --to-revision=42

# Watch until ready
kubectl rollout status deployment/page-pulse
```

On platforms without native undo (Render, Railway, Fly): redeploy the previous
image tag. **Image tags are immutable digests**, never `latest`, so "previous"
is always an exact binary.

### Store / migration rollback

Rules that keep this boring:

1. Additive-only schema changes in production.
2. Every migration ships with a down script tested in staging.
3. Dual-write for at least one deploy cycle before dropping an old field.

Because cache entries and rate-limit counters are ephemeral (TTL ≤ 1 hour), a
rollback never needs to repair them — they expire on their own.

### Communication

| Moment | Channel | Content |
|---|---|---|
| Deploy starts | `#deploys` | version, change link, rollback owner |
| Rollback starts | `#incidents` | reason, ETA, owner |
| Rollback done | `#incidents` | confirmed version, current error rate |
| Within 24 h | post-mortem doc | timeline, root cause, action items |

---

## 7. Post-deploy checklist (human)

- [ ] Deploy annotated on the overview dashboard
- [ ] Smoke tests green
- [ ] Error rate and p95 within 2× of pre-deploy baseline for 10 minutes
- [ ] No new WARNING+ log patterns in the last 5 minutes
- [ ] Cache hit rate not collapsed
- [ ] On-call knows the change set

---

## 8. Capacity review cadence

| Cadence | Question |
|---|---|
| Weekly | Top slow hosts, clients hitting the rate limit, cache hit trend |
| Monthly | Do alert thresholds still match traffic? Any silent pages? |
| Quarterly | Load-test 2× the current peak; disaster-recovery drill |

---

**Document version:** 1.0 · **Tied to:** architecture.md §5–§6
