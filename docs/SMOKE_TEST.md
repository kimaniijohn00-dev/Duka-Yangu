# Duka-Yangu HTTP smoke test

One-command, end-to-end check of the real v2 server over HTTP: auth, per-shop
isolation, passports, the public lender link, CSRF, invite codes, cookies and
login lockout. It starts its own server and cleans up after itself.

## Run it

From the repo root:

```powershell
powershell -ExecutionPolicy Bypass -File backend/scripts/smoke_http.ps1
```

Requirements:

- Python with the backend dependencies (the repo `.venv` is used when
  present, otherwise `python` on PATH). No Meta/Spark API key is needed.
- Windows PowerShell 5.1 or later (written for 5.1 compatibility).
- Nothing else running on the chosen port (the script picks a free one).
- No server needs to be running; the script starts its own.

The script prints one `PASS`/`FAIL` line per check with the expected and
actual status code, then `Summary: PASS n, FAIL m`. Exit code is 0 when every
check passes, 1 otherwise. It never prints a full token, PIN hash or invite
code (first 4 chars plus length only), and never prints a PIN.

What it does not do: no deployment, no repo changes, no pytest run, no temp
folders inside the project. The throwaway database and server logs live in
the system temp directory and are deleted at the end, even on failure.

## How it works

1. Creates a throwaway database file in the system temp directory (never
   `backend/duka.db`) and picks a free localhost port.
2. Starts `uvicorn app:app` itself as a child process with
   `DUKA_ENV=development`, a random 32-byte `DUKA_SESSION_SECRET`,
   `DUKA_ALLOWED_ORIGINS` set to the script's own origin, and `DUKA_DB_PATH`
   pointing at the temp database; waits up to 45 s for `/api/health`.
3. Creates two single-use invite codes with `backend/scripts/make_invite.py`
   against the temp database, then registers Shop A and Shop B over HTTP,
   each with its own cookie jar and the `Origin` header.
4. Seeds data with `backend/scripts/smoke_seed.py` (plain SQL, no AI parser):
   Shop A gets 60 days x 3 confirmed rows (180 total, enough for a credit
   score) plus one unconfirmed draft; Shop B gets one unconfirmed draft.
5. Runs the checks below, stops the server, deletes the temp files.

## What each check proves

| Check | Proves |
| --- | --- |
| Anonymous `POST` to entry, confirm, transcribe and passport-create, plus anonymous `GET` of ledger, passport list, revoke and activity, all return 401 | Every login-required endpoint rejects unauthenticated callers, so anonymous writes are impossible |
| Anonymous `GET` of credit summary and weekly dashboard return 200 with the demo shop; a logged-in caller gets their own shop | The demo exception is narrow (read-only demo rows for strangers, own rows for members) |
| Shop A reads 180 own ledger rows (181 after confirming) with no B rows; Shop B's ledger is empty and holds no A rows | Ledger reads are scoped to the session shop; drafts never leak into ledgers |
| Shop B confirming Shop A's draft gets 404, identical to a missing id; Shop A confirming it works | Cross-shop writes are indistinguishable from missing rows (no id oracle), while own workflows function |
| Shop A creates a passport; Shop B's list omits it; Shop B revoking it or reading its activity gets 404; Shop A revokes it | Passports are per-shop for list, revoke and activity |
| The public lender link returns 200 with no login, 410 after revoke, and an unknown token returns 404 | Lender links stay login-free and die on revoke without leaking other states |
| `GET` ledger/summary with `?shop_id=B` still return Shop A's data; `POST` passports with `shop_id=B` in the body creates a passport owned by Shop A only | A client-supplied shop id is ignored everywhere, in query and body |
| `POST` entry, confirm and transcribe with no `Origin`, and `POST` confirm with a foreign `Origin`, all return 403, and the AI usage counters do not move | State-changing requests require a same-origin (or allow-listed) origin, containing CSRF with cookie auth; rejection happens before any AI call, quota charge or write |
| Reusing Shop A's code and a wrong code both return the same generic 403; a weak PIN returns 422; the same code then registers Shop C with a strong PIN | Codes are single-use, errors reveal nothing, weak PINs never consume a code |
| Login `Set-Cookie` carries HttpOnly and SameSite=Lax, and no Secure flag in development mode | The session cookie has its hardening flags, with the documented local-http exception |
| Logout returns 200; anonymous `GET /api/auth/me` and a replay of the pre-logout cookie both return 401 | Logout revokes the session server-side, it is not just a client-side cookie delete |
| Three wrong PINs for Shop C return 401, then the correct PIN also returns 401 | The (phone, IP) lockout triggers after repeated failures and holds while locked |

## Status codes on both PowerShell editions

The script runs on Windows PowerShell 5.1 and PowerShell 7+. On 7+ it uses
`-SkipHttpErrorCheck`, so HTTP error statuses arrive on the response object
and never pass through exception plumbing. On 5.1 it reads the status from
the exception's response object, with the `(403)`-style message text as a
fallback. A `-1` is never silent: the helper prints the exception message
and up to 500 chars of the response body, and the check FAILs with a clear
label. A `-1` therefore means the request never produced a readable HTTP
response (transport failure, proxy interference, or timeout) — not that the
app returned an odd status.

## Ruling out implicit headers (section 9b)

The no-Origin probes must prove the app rejects requests that truly carry no
`Origin` and no `Referer` — not requests where the HTTP stack quietly added
one. So the script prints exactly what it hands the stack (an empty header
hashtable, or no headers at all, plus the login cookie) and the session's
default-header count, then repeats all three probes through a bare
`System.Net.Http.HttpClient` with zero default request headers and only the
copied cookies. HttpClient cannot add `Origin` or `Referer` on its own.
Reading the verdict: if the HttpClient probes return 403 while the
Invoke-WebRequest ones pass, the extra header came from the
Invoke-WebRequest stack; if both pass, it came from the path between client
and server (proxy, filter, or agent) — the app itself rejects headerless
requests, proven in-process with byte-exact header visibility.

## Rate limits

The script stays silent on purpose: exactly 5 counted registrations per hour
(limit 10), 2 invite guesses (limit 10), 5 logins per minute (limit 20), 4
per-phone failures in 24 h (limit 30), 0 admitted entry/transcribe/explain
calls, 2 passport creates (limit 10/min) and 3 public lender views
(limit 30/min). The missing-Origin probes are rejected before any guard,
charge or write, so they consume no quota (the counters check proves it).
See the comment block at the top of `smoke_http.ps1` for the
exact accounting. If a limit is ever tightened below these counts, the script
will report the resulting 429s as failures rather than silently passing.

## Troubleshooting

- `server did not answer /api/health`: the script prints the server's stderr
  tail. The usual cause is a missing dependency (run from the repo root so
  `.venv` is found) or a port blocked by a firewall.
- Any `FAIL` line names the check and shows expected vs actual status; rerun
  after fixing the app. The temp database is deleted on every run, so reruns
  start clean.
- `NO HTTP STATUS for <uri>`: the request failed below HTTP (connection
  refused/reset, timeout, or a proxy swallowing localhost). Check for a
  machine proxy env (`HTTP_PROXY`/`HTTPS_PROXY`, bypass localhost), a
  firewall, or the server's stderr tail. The printed exception message says
  which one it was.
- The script uses only localhost and never contacts Meta: if a check fails
  with 502 from `/api/entry` or `/api/transcribe`, an authenticated call is
  reaching the AI layer, which means the script (not the app) regressed.
