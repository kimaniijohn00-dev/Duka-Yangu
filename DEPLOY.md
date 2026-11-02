# Deploying Duka-Yangu on Modal

One Modal app (`duka-yangu`) serves both the FastAPI backend and the built
frontend from a single web URL. The SQLite file lives on a Modal Volume,
so data survives restarts and redeploys.

## 0. Prerequisites

- Python 3.12+ and Node.js 18+ installed.
- A Modal account (sign up at <https://modal.com>).

All commands below run in Windows PowerShell.

## 1. Install the Modal CLI and log in

```powershell
cd C:\Users\John Mbithe\Desktop\Duka-Yangu
pip install modal
modal setup
```

`modal setup` opens a browser window to authenticate this machine.

## 2. Create the `duka-yangu` secret in the Modal dashboard

1. Open <https://modal.com/secrets> (Secrets page of the dashboard).
2. Create a new secret named exactly `duka-yangu`.
3. Add two keys:
   - `MODEL_API_KEY` — your Meta Model API key.
   - `SPARK_MODEL` — the Spark chat model name (for example `muse-spark-1.3`).
4. Save. The app injects both as environment variables; no keys live in
   code or in the image.

## 3. Build the frontend

The image bundles `frontend/dist`, so build it before every deploy:

```powershell
cd C:\Users\John Mbithe\Desktop\Duka-Yangu\frontend
npm install
npm run build
```

`modal_app.py` refuses to build the image when `frontend/dist` is missing.

## 4. Deploy

```powershell
cd C:\Users\John Mbithe\Desktop\Duka-Yangu
modal deploy modal_app.py
```

On the first deploy the command prints the public web URL, roughly
`https://<workspace>--duka-yangu-web.modal.run`. Open it in a browser.
What gets created automatically:

- The `duka-yangu-data` Volume for the SQLite file
  (`DUKA_DB_PATH=/data/duka.db`). You can also create it up front with
  `modal volume create duka-yangu-data`.
- A single container (`max_containers=1`, so SQLite has one writer) that
  scales to zero after 300 idle seconds (`scaledown_window=300`).

## 5. Verify the deployment

- Open the printed URL: the shop app loads.
- Open `<url>/api/health`: returns `{"status":"ok"}`.
- Create a Business Passport and open its `/p/<token>` link directly in a
  fresh tab: it loads (SPA fallback), and reloads work.
- On first start the demo shop is seeded once; existing data is never
  re-seeded or touched.

## 6. Cost protection (already wired in)

- Per-IP rate limits: `POST /api/entry` 20/min, `POST /api/transcribe`
  10/min, `GET /api/credit/explain` 10/min, `POST /api/passports` 10/min.
- Global daily budget for Spark + voice calls combined, default 400/day
  stored in SQLite so it survives restarts. When exhausted, API callers
  get `429 "Demo limit reached for today, please try again tomorrow"`,
  while the Credit tab still returns a 200 template explanation.
- To change the cap, set the `DUKA_DAILY_AI_CAP` environment variable
  (for example add it to the `duka-yangu` secret, or to the image `.env`
  in `modal_app.py`) and redeploy.

## 7. Local development (unchanged)

```powershell
# Terminal 1 — backend (uses backend/duka.db by default)
cd C:\Users\John Mbithe\Desktop\Duka-Yangu\backend
python -m uvicorn app:app --reload --port 8000

# Terminal 2 — frontend dev server (proxies /api to the backend)
cd C:\Users\John Mbithe\Desktop\Duka-Yangu\frontend
npm run dev
```

Run the backend tests from `backend/` with `python -m pytest tests/ -q`.
