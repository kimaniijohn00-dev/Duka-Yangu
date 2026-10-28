# Duka-Yangu Frontend (Day 3A)

Mobile-first, typed-entry-only page (no voice yet). React + TypeScript + plain
CSS, no extra libraries. In dev, Vite proxies `/api` to the FastAPI backend,
so the backend needs no CORS changes.

## Run the backend (terminal 1)

```powershell
cd C:\Users\John Mbithe\Desktop\Duka-Yangu\backend
..\.venv\Scripts\Activate.ps1
uvicorn app:app --reload --port 8000
```

The API is then at `http://127.0.0.1:8000`.

## Run the frontend (terminal 2)

```powershell
cd C:\Users\John Mbithe\Desktop\Duka-Yangu\frontend
npm install
npm run dev
```

Open `http://localhost:5173` in a browser (narrow it to ~390px wide for the
phone layout, or use device emulation).

## How it works

- **Entry box:** type e.g. `sold 5kg sugar for 800 bob`, tap Add. This calls
  `POST /api/entry`, which saves a draft (`confirmed = 0`).
- **Read-back card:** shows what Spark understood in a plain sentence, e.g.
  “Sale: 5 kg sugar for KES 800.” Edit lets you change item, quantity and
  amount before confirming; Confirm sends the edited values with
  `POST /api/transactions/confirm`. Nothing reaches the ledger until confirm.
- **Ledger:** loads `GET /api/ledger` (confirmed entries, newest first) with
  today’s sales total (`sale`) and spent total (`expense` + `purchase`).
  It refreshes after every confirm.

Confirm persists any edited item, quantity and amount to the transaction
before marking it confirmed, and the ledger shows the edited values.

## Build (type-check + production bundle)

```powershell
cd C:\Users\John Mbithe\Desktop\Duka-Yangu\frontend
npm run build
```

No secrets live in this folder. `.env` is never read here.
