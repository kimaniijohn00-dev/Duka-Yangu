# Duka-Yangu v2 plan — real accounts and per-shop data isolation

Status: planning only. No code was written, nothing was deployed to Modal,
and nothing was committed in this step.

Working assumption: this plan targets the `v2` branch. Git could not be
consulted from this environment (ownership block), so confirm with
`git branch` before implementing.

## 0. Where v1 stands (evidence, not assumptions)

- There is no authentication anywhere. [backend/app.py](backend/app.py)
  trusts caller-supplied `shop_id` (`EntryIn.shop_id`, query params) and
  falls back to the demo shop (`_resolve_shop_row`) or to `shop_id IS NULL`
  rows (`read_ledger` with no argument).
- Tables in [backend/db.py](backend/db.py): `shops`, `transactions`
  (`shop_id` nullable), `products` (`shop_id` nullable, no endpoint uses
  it), `audit_log` (no shop column), `passports` (`shop_id` nullable in
  practice), `passport_views`, `usage_counters` (global), `explain_cache`
  (already keyed by shop).
- The frontend ([frontend/src/api.ts](frontend/src/api.ts)) never sends
  `shop_id` or credentials; [frontend/src/App.tsx](frontend/src/App.tsx)
  routes `/p/<token>` to a public lender page that must stay login-free.
- Tests ([backend/tests/](backend/tests/), 10 files) call route functions
  directly against temporary DBs. [backend/pytest.ini](backend/pytest.ini)
  pins `--basetemp=.pytest_tmp` inside the repo: every future test run
  must override it with `--basetemp` pointing outside the repo (system
  temp directory).
- [backend/requirements.txt](backend/requirements.txt) has no password or
  token libraries, and
  [test_modal_app.py](backend/tests/test_modal_app.py) forces
  [modal_app.py](modal_app.py) `PYTHON_PACKAGES` to match it exactly.

## 1. Auth: phone number plus PIN (no SMS)

- New endpoints: `POST /api/auth/register {phone, pin, shop_name?}`
  creates one `shops` row plus one `users` row in a single transaction;
  `POST /api/auth/login {phone, pin}` creates a server-side session and
  sets the session cookie; `GET /api/auth/me` returns the current shop;
  `POST /api/auth/logout` revokes the session server-side and clears
  the cookie.
- New `users` table: `id`, `shop_id` (FK, NOT NULL), `phone` (UNIQUE,
  stored canonical: digits only, accept `07…`, `254…`, `+254…`),
  `pin_hash`, `created_at`. One phone maps to one shop for v2.
  Throttling state lives in `login_throttles`, not here.
- PIN policy: minimum 6 digits (6–12 allowed), plus a small blocklist
  (`000000`, `123456`, all-same-digit, simple sequences). Six digits is
  the floor because short PINs fall fast to guessing; brute force beyond
  that is stopped by lockout, not by PIN complexity.
- PIN hashing with a vetted library only: Argon2id preferred, bcrypt
  acceptable. No home-made crypto: randomness only from `secrets`,
  hashing and token signing only from vetted libraries.
- Token handling decision: signed session token in an httpOnly,
  SameSite=Lax cookie — not an in-memory Bearer token. Justification:
  the backend serves the frontend from the same origin, so the cookie
  is sent automatically and survives reloads (an in-memory token would
  log the shopkeeper out on every refresh); httpOnly keeps the token
  out of JavaScript reach, so ordinary XSS cannot steal it; CSRF is
  contained because the API is same-origin-only and a Lax cookie is
  never sent cross-site.
- The token is signed with the server secret (see §6) and carries only
  `{session_id, shop_id, exp}`, with 30-day expiry. Sessions are also
  recorded server-side in a new `sessions` table (token hash, user,
  shop, created/expiry/revoked timestamps), so logout and operator
  actions revoke immediately: every request verifies the signature and
  checks the row is live. Logout revokes the row and clears the cookie;
  a PIN reset revokes all of the shop's sessions.
- Login rate limiting plus temporary lockout, both server-side, keyed
  by phone number AND client IP together with growing delays (for
  example 1 minute after 3 fails, 5 after 5, 15 after 8; exact rungs
  set in implementation). Keying on the pair means an attacker who
  knows only the victim's number merely locks out their own IP — the
  victim logging in from elsewhere is unaffected. Counters live in a
  `login_throttles(phone, ip, fails, locked_until)` table so restarts
  do not clear them. The existing per-IP sliding-window limit stays as
  a backstop against one IP spraying many numbers. Failures return a
  generic 401 that does not reveal whether the phone is registered
  (verify a dummy hash for unknown phones).
- Account recovery (pilot, no SMS): manual operator reset, in person
  only. The operator verifies the person, sets a temporary PIN, clears
  throttles, and revokes all of the shop's sessions; every reset is
  written to `audit_log`. No self-serve reset exists in v2.
- Phone numbers are unverified until SMS exists. Nothing — UI copy, API
  responses, passport snapshots — may present a number as verified: no
  "verified" badges, labels, or fields. Copy review for this rule is
  part of the frontend step.

## 2. What becomes shop-scoped, and how the tie works

- Core principle: the server derives `shop_id` exclusively from the
  session token. Any client-sent `shop_id` is ignored (preferred) or
  rejected, never trusted. Missing token gives 401; a valid token
  touching another shop's row gives 404 (not 403, to avoid leaking
  row existence).
- Tables: `transactions.shop_id` becomes NOT NULL plus an index;
  `products.shop_id` becomes NOT NULL; `passports.shop_id` becomes
  NOT NULL (`token_hash` stays UNIQUE); `explain_cache` stays
  shop-keyed and is only ever read with the session shop;
  `audit_log` gains a nullable `shop_id` (NULL marks public or
  anonymous events such as passport views); `shops` gains
  `is_demo INTEGER NOT NULL DEFAULT 0` (see §3); new `users`,
  `sessions`, and `login_throttles` tables from §1.
- AI budget: `usage_counters` keeps its global daily row AND gains
  per-shop rows (composite key on day, name, shop), so one shop cannot
  burn everyone's budget. The global cap stays the backstop; each shop
  gets its own smaller daily quota (tunable, for example via
  `DUKA_SHOP_AI_CAP`). Both are checked before every Spark/voice call;
  the template-explanation fallback for the Credit tab is unchanged.
- Endpoints that require auth and use only the session shop:
  `POST /api/entry`, `POST /api/transactions/confirm` (must also check
  the row's `shop_id` against the session), `GET /api/credit/summary`,
  `GET /api/dashboard/weekly`, `GET /api/credit/explain`,
  `GET /api/ledger`, `POST`/`GET /api/passports`,
  `POST /api/passports/{id}/revoke`, `GET /api/passports/{id}/activity`.
  Stay public: `GET /api/p/{token}`, `GET /api/health`, the static
  frontend, and the demo reads in §3.
- One shared dependency (for example `require_shop`) reads the session
  cookie, verifies the signature and the live `sessions` row, and
  returns the session shop for all of the above, so scoping cannot be
  forgotten per endpoint. Tests keep the existing direct-call style by
  passing the session explicitly instead of going through cookies.

## 3. Demo shop and lender links without login

- The demo shop stays public and read-only, marked by an `is_demo`
  flag on the `shops` row instead of name matching: the four read
  endpoints (`credit/summary`, `dashboard/weekly`, `credit/explain`,
  `ledger`) serve without a token only when the resolved shop has
  `is_demo = 1`; any other shop without a token gets 401. The flag is
  data, so it works identically on the local `duka.db` and the Modal
  volume's `/data/duka.db`: the migration (§4) sets `is_demo = 1` on
  the existing demo row once, and `ensure_demo_seed` sets it on every
  fresh seed. Demo reads stay read-only with no writes, and no new
  shop can gain the flag through any API.
- All writes (`entry`, `confirm`, `transcribe`, passport creation)
  require login. The "try it in 2 minutes" flow becomes register-first,
  which stays low-friction because there is no SMS step.
- Lender links keep byte-for-byte behavior: token-hash lookup, expiry
  and revoke checks, view logging in `passport_views`/`audit_log`.
  Regression tests must prove no auth requirement leaked onto
  `GET /api/p/{token}`. Demo pinning (`resolve_as_of`) and demo
  seeding (`scripts/seed_demo.py`) are untouched.

## 4. Migrating existing data safely

- Current data to preserve: the demo shop row (fixed id `999999`, with
  autoincrement fallback in `seed_demo.py`) plus its transactions, and
  real entries stored with `shop_id NULL` (pre-v2 `EntryIn` never sent
  a shop id, so every genuine user entry is a NULL row).
- Procedure: back up the database file first (on Modal, snapshot the
  `duka-yangu-data` volume before migrating; deploy itself is a later
  step). Then run a new idempotent `scripts/migrate_v2.py`: create the
  new tables (`users`, `sessions`, `login_throttles`) and columns
  (`audit_log.shop_id`, `shops.is_demo`, per-shop `usage_counters`
  support), add indexes, set `is_demo = 1` on the existing demo row
  (matched by name exactly once, then the flag is the source of truth),
  and move every `shop_id NULL`
  transaction into a reserved shop row such as "Pre-v2 entries
  (unclaimed)", which is never publicly listed. A small operator-run
  claim step reassigns those rows to the owner's newly registered shop
  (explicit id list, logged). `NOT NULL` constraints go on only after
  the backfill, never before.
- Verification is by counts: zero NULL `shop_id` rows remain, per-shop
  transaction totals equal pre-migration totals, and demo rows are
  byte-identical. New signups always create fresh shops and never reuse
  demo or unclaimed ids.

## 5. Order of work (small, each independently testable)

Run every suite as `pytest --basetemp <system-temp-dir>` (for example
`--basetemp=$env:TEMP/duka-tests` in PowerShell), never the repo-default
`.pytest_tmp`. Each step lands with tests before the next begins.

Isolation test rule (applies to steps 4–6): every shop-scoped endpoint
gets a two-shop test — two shops seeded, session of shop A, B's rows —
proving A cannot read, modify, list, or revoke anything of B's,
including confirm/revoke/activity on B's ids addressed by number.

1. Dependencies and secrets: add the Argon2/bcrypt and token libraries
   to `requirements.txt` (keeping `modal_app.py` parity); secrets come
   only from the environment, never from code (see §6).
2. Schema: new `users`, `sessions`, `login_throttles` tables,
   `audit_log.shop_id`, `shops.is_demo`, per-shop counter support — on
   empty temporary DBs, no behavior change yet.
3. Auth endpoints plus throttling: register, login, `me`, logout; tests
   for the 6-digit minimum, (phone, IP) lockout with growing delays,
   restart persistence, no phone-exists oracle, and cookie flags.
4. One shared cookie-reading `require_shop` dependency; scope `ledger`
   and `entry` first, each with its two-shop test.
5. Scope `confirm` and the credit trio and all passport endpoints, each
   with its two-shop test (read, modify, list, revoke).
6. AI quotas: global cap plus per-shop quota, with tests proving one
   shop's usage neither touches another shop's quota nor breaks the
   Credit-tab template fallback.
7. Demo-public rules on the `is_demo` flag plus lender-link regression
   tests (public reads work, public writes fail, `/p/{token}` untouched).
8. Migration script plus a dry run against a database copy, with the
   count checks from §4 (including `is_demo = 1` set exactly once).
9. Frontend: register/login screens, cookie-based calls over the same
   origin, 401 redirect to login, no "verified" phone copy anywhere,
   demo-mode banner for anonymous readers.
10. Docs (`README.md`/`DEPLOY.md` deltas) and a rollout checklist:
    volume backup, Modal secret wiring, deploy — deployment stays a
    later step, not this one.
11. Later (after rollout): "delete my account and data" — export one
    shop's data (transactions, passport metadata, credit summary) as a
    file, then hard-delete its rows (`transactions`, `passports` and
    views, `explain_cache`, `users`, `sessions`, shop audit rows) with
    row-count verification; the demo shop can never be deleted this way.

## 6. Signing key and secrets

- Secrets live only in the Modal Secret named `duka-yangu`
  (`DUKA_SESSION_SECRET` for cookie signing, plus the existing
  `MODEL_API_KEY`/`SPARK_MODEL`); never in code, config files, logs, or
  the repo. Local development reads the same names from `.env`, which
  `.gitignore` already excludes. A test asserts no secret value appears
  in the codebase.
- Rotation procedure: generate a new secret, add it to the Modal Secret
  alongside the old one, deploy, then mark the new key primary so fresh
  logins sign with it while old cookies still verify during a grace
  window (shorter than the 30-day session expiry); after the window,
  revoke remaining old sessions and remove the old key. Rotation is
  rehearsed with the two-shop tests passing throughout.

## 7. Main risks

- PINs are only 6+ digits, so the (phone, IP) throttle with growing
  delays is load-bearing: it must be server-side, restart-proof, and
  covered by tests, without letting an attacker lock out the victim.
- Session cookies are httpOnly but still bearer-equivalent on the
  machine: keep expiry bounded, payload minimal, and SameSite=Lax
  intact; any future cross-origin API use reopens CSRF and needs its
  own review.
- Any endpoint missing the shared dependency is a cross-shop leak, so
  every scoped endpoint gets a cross-shop test and client `shop_id`
  is ignored everywhere.
- The demo exception must stay narrow (`is_demo = 1`, read-only, no
  API can set the flag); otherwise it becomes a hole.
- Migration can lose real entries: backup first, idempotent script,
  count verification, demo rows never rewritten.
- No SMS in v2 means no self-serve PIN reset; forgotten PINs need a
  documented manual reset, not a built recovery flow.
- Quota tuning can starve real shops (too low) or the shared budget
  (too high); start conservative per shop and watch the counters.
- Unverified-number copy slipping into the UI or snapshots, and secrets
  slipping into code or logs, are both review-gated in steps 9–10.
- Non-goals for v2: SMS verification, roles or multi-user shops, magic
  links, and any Modal deployment work.
