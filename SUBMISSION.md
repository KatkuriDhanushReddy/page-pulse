# Digital Heroes SDE — submission pack

Use this as the checklist when you DM [@realshreyanshsingh](https://instagram.com/realshreyanshsingh).

## Links to send (not files)

| Item | URL |
|---|---|
| Public GitHub repo | https://github.com/KatkuriDhanushReddy/page-pulse |
| Live deployed service | `https://<your-service>.onrender.com` _(after Render deploy)_ |
| Architecture (Task B) | https://github.com/KatkuriDhanushReddy/page-pulse/blob/main/docs/architecture.md |
| Observability / rollback | https://github.com/KatkuriDhanushReddy/page-pulse/blob/main/docs/observability.md |
| AI usage note | https://github.com/KatkuriDhanushReddy/page-pulse/blob/main/docs/ai-usage.md |
| CI (green) | https://github.com/KatkuriDhanushReddy/page-pulse/actions |

Suggested DM text:

> Hi — SDE qualification submission (Role 03).
> Live: \<LIVE_URL\>
> Repo (tests + CI): \<GITHUB_URL\>
> Task B docs are in `/docs` on the repo.
> AI usage: `/docs/ai-usage.md`

## Before you hit send

- [ ] Footer on the live site shows **Built for Digital Heroes Training Task** linking to [digitalheroesco.com](https://digitalheroesco.com)
- [ ] `GET /api/health` on the live URL returns `"status": "ok"`
- [ ] GitHub Actions is green on the default branch
- [ ] README top table has the live URL filled in
- [ ] Repo is **public**
- [ ] You followed `@realshreyanshsingh` (submission rule)

## Deploy in ~10 minutes (Render)

1. Push this folder to a new public GitHub repo.
2. [Render](https://render.com) → New → Web Service → connect the repo.
3. Runtime: **Docker** (uses the included `Dockerfile`).
4. Set env (optional for demo): leave `MONGO_URL` empty.
5. After deploy, open the URL, confirm the footer, paste it into `README.md`.

Railway / Fly work the same way from the Dockerfile.

## Local verify

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements-dev.txt
pytest --cov=app
uvicorn app.main:app --port 8000
```
