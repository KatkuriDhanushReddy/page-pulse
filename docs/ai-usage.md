# How AI was used on this submission

Digital Heroes asks candidates to use AI properly and say how. This is that note.

## What AI did

- Scaffolded the FastAPI project layout and first-pass module split.
- Drafted large sections of `docs/architecture.md` and `docs/observability.md`.
- Generated the initial pytest skeletons and CI workflow YAML.
- Produced the first version of the static console (`static/index.html`).

## What I owned / changed

These decisions are mine; several of them disagree with the first AI draft:

| Decision | Why it is not the AI default |
|---|---|
| Failures are **not** cached | An early draft cached every result. A transient timeout would then be served for the full TTL. |
| Batch costs **N** rate-limit units | Drafts charged 1. That lets a client bypass the limit by batching. |
| SSRF guard on by default | Drafts fetched any hostname, including `localhost` / RFC1918. |
| Structured 4xx for *our* errors, 200 + `error` object for *target* failures | Keeps "audit completed" distinct from "API broke". |
| In-memory default + Mongo optional | Lets CI and a single-instance live demo run with zero infra, without lying about multi-replica needs. |
| `selectolax` instead of BeautifulSoup | HTML parsing is the only CPU work on the event loop; the faster parser matters under the 500-concurrent burst. |
| Architecture doc sized to the **burst**, not the daily average | 10k/day is trivial; 500 concurrent is the real constraint. |

## How to verify the work is mine

1. Read `docs/architecture.md` §3–§5 — the queueing argument and the three failure modes are written against this codebase's actual knobs (`AUDIT_MAX_CONCURRENCY`, `CACHE_TTL_SECONDS`, fail-open rate limiter).
2. Run `pytest` — the suite asserts the decisions above (no-cache-on-failure, batch costing, SSRF, semaphore ceiling).
3. Hit the live URL footer: **Built for Digital Heroes Training Task** → `digitalheroesco.com`.

AI accelerated typing. It did not choose the trade-offs, and it is not what the tests protect.
