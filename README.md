# Duka-Yangu

**We turn a shopkeeper's voice into a credit history.**

Built for the Meta Global AI Developer Hackathon: *Build for Your World*, Indie Developer track. Solo project by [@kimaniijohn00-dev](https://github.com/kimaniijohn00-dev).

## Live demo

**https://kimaniijohn00-dev--duka-yangu-web.us-east.modal.run**

The first load can take up to 30 seconds while the container wakes up. The demo runs on **synthetic data**.

### Try it in 2 minutes

1. On the **Entries** tab, tap the mic (or type) and say: *"Sold five kilos of sugar for 800 bob."*
2. Check the read-back card, then tap **Confirm**. It appears in the ledger.
3. Open the **Credit** tab: a score out of 100, the factors behind it, a safe loan estimate, and a plain-English explanation.
4. Open the **Passport** tab: create a link for a lender, open it in a private window, then **Revoke** it and reload. It stops working immediately.

## The problem

Kenya has about 7.4 million micro, small and medium enterprises (KNBS 2016 MSME Survey, [report](https://www.knbs.or.ke/2016-micro-small-and-medium-enterprises-msme-survey-basic-report-2/); the count dates from 2016). According to a 2024 Stears report, covered by The Star ([Kenya's small ventures lack formal credit access](https://www.the-star.co.ke/business/kenya/2024-06-07-kenyas-small-ventures-lack-formal-credit-access--survey)), only about 1.1 million of them (roughly 16%) can access formal credit. The Kenya Bankers Association's 2021 MSME survey reports that firms struggle with the documents lenders require and with collateral.

A duka keeper who sells every day still looks invisible on paper. Duka-Yangu builds a record a lender can trust from something shopkeepers already do: talk.

## What it does

1. The shopkeeper speaks or types what happened, for example "sold 5kg sugar for 800 bob".
2. Muse Spark parses it into a structured transaction (sale, purchase, expense, credit given, credit received).
3. A read-back card shows what was understood. The shopkeeper confirms or edits it first.
4. Confirmed entries land in the ledger. Drafts never appear there.
5. The Credit tab shows a credit-readiness score (0-100), the factors behind it, and a conservative safe loan estimate.
6. The shopkeeper can share a consent-based **Business Passport**: a read-only link a lender opens to see a summary.

## How it uses Meta's models

- **Muse Spark 1.3** parses free text into a transaction through a `record_transaction` tool call. Its system prompt teaches it Kenyan trader phrasing ("bob" means shillings, "2k" means 2000). It also writes the plain-English explanation of the credit score.
- **Muse Voice Transcribe** (`muse-voice-transcribe-1.0`) turns a short voice recording into text for the entry box.

**The credit score and the loan size are computed by plain deterministic Python** in [backend/services/credit.py](backend/services/credit.py). An LLM never calculates them. Spark only explains the result, and every number in its explanation is checked against the real data. If the reply contains a number that is not in the data, or the call fails, the app falls back to a fixed template built from the same numbers.

## Privacy and safety design

- Nothing reaches the ledger until the shopkeeper confirms it. Unconfirmed drafts stay out of the score, the loan estimate and every passport.
- Audio is processed in memory only. It is never written to disk, the database or logs.
- A passport is a snapshot of summary numbers: no raw transactions and no customer names.
- Share tokens are random (`secrets.token_urlsafe(32)`) and only their SHA-256 hash is stored. The raw token is shown once and never logged.
- Passports expire (7 or 30 days), can be revoked in one tap, and every view and action is recorded in a view log and an audit log.
- The public demo is protected by per-IP rate limits (entry 20/min, transcription 10/min, explanation 10/min, passport creation 10/min, passport views 30/min) and a shared daily cap on AI calls (400/day by default). When the cap is reached, the Credit tab still answers with the template explanation instead of failing.

## What was built during the hackathon

Everything in this repository was built between Oct 26 and Nov 3, 2026. The commit history shows the order of work:

```powershell
git log --oneline
```

It moves from the text entry loop, through voice entry and the credit engine, to passports and the Modal deployment.

## Architecture

```text
Shopkeeper (React + TypeScript + Vite)
  |  voice clip / typed text
  v
FastAPI backend
  |-- POST /api/entry      -> Muse Spark 1.3 parses -> draft (confirmed = 0)
  |-- POST /api/transcribe -> Muse Voice Transcribe -> text (audio never stored)
  |-- POST /api/transactions/confirm -> draft becomes ledger (edits persisted)
  |-- GET  /api/credit/summary, /api/credit/explain -> deterministic score + loan
  |-- POST /api/passports, GET /api/p/{token}      -> hashed-token lender links
  v
SQLite (one file, persisted on a Modal Volume; a single container as the only writer)
```

Stack: FastAPI, SQLite, React + TypeScript + Vite, deployed on Modal (`modal_app.py`, see [DEPLOY.md](DEPLOY.md)).

## Limitations and next steps

These are scope decisions for a one-week prototype:

- **Synthetic demo data.** The demo shop is seeded once with 60 days of generated sales. Entries you type are stored separately and never mix with it.
- **No user accounts yet**, so all visitors share one demo shop. Next: phone-number sign-in and a private ledger for each shopkeeper.
- **The score is a transparent heuristic** (record-keeping regularity, sales stability, profitability, repayment behaviour, trend). It is not a validated lending model or a lending decision. The Credit tab and the lender page both say so.
- **English only** for now. Swahili is a natural next step.

## Run locally

Prerequisites: Python 3.12+ and Node.js 18+. Create a `.env` file in the project root (never commit it) with:

- `MODEL_API_KEY`: your Meta Model API key
- `SPARK_MODEL`: for example `muse-spark-1.3`

**Windows PowerShell**, from the project root:

```powershell
cd backend
python -m venv ..\.venv            # first time only
..\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python scripts/seed_demo.py        # first time only: creates the synthetic demo shop
python -m uvicorn app:app --reload --port 8000
```

In a second window, from the project root:

```powershell
cd frontend
npm install
npm run dev
```

**macOS / Linux**, from the project root:

```bash
cd backend
python3 -m venv ../.venv && source ../.venv/bin/activate
pip install -r requirements.txt
python scripts/seed_demo.py
python -m uvicorn app:app --reload --port 8000
```

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:5173`. The dev server proxies `/api` to the backend, so no CORS setup is needed.

To wipe and regenerate only the demo shop's data: `python scripts/seed_demo.py --reset` (from `backend/`).

**Run the tests** (from `backend/`):

```bash
python -m pytest tests/ -q
```

## Credits

Built with Meta's Muse models. Hosted on Modal.

License: [MIT](LICENSE)