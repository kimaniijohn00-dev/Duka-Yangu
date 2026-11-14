# Duka-Yangu endpoint matrix (plan steps 4, 7–8)

Status: demo reads are served by the `shops.is_demo` flag (set by the
seeder and the migration); pre-v2 `NULL`-shop data is unreachable until
`scripts/migrate_v2.py --apply` moves it to the unclaimed shop.

Every endpoint, its auth rule, and how its shop is resolved. The shop id
always comes from the logged-in session; no endpoint accepts a
client-supplied shop id in any form. Enforced by
[backend/tests/test_isolation.py](../backend/tests/test_isolation.py)
(`test_route_inventory_guard`).

| Method | Path | Auth rule | Shop resolution | Origin check |
| --- | --- | --- | --- | --- |
| POST | `/api/auth/register` | public (invite code) | creates a new shop (`is_demo = 0`; demo name rejected with 422) | yes |
| POST | `/api/auth/login` | public (rate limited) | session created for the user's shop | yes |
| POST | `/api/auth/logout` | login required | revokes the session's shop session | yes |
| GET | `/api/auth/me` | login required | session shop | no (safe method) |
| POST | `/api/entry` | login required | session shop; draft saved under it | yes |
| POST | `/api/transactions/confirm` | login required | row must belong to session shop, else 404 | yes |
| POST | `/api/transcribe` | login required | charged to session shop quota | yes |
| GET | `/api/ledger` | login required | session shop only (NULL-shop rows unreachable) | no (safe method) |
| GET | `/api/credit/summary` | demo-read | session shop; `?view=demo` forces demo; anonymous gets demo | no (safe method) |
| GET | `/api/dashboard/weekly` | demo-read | session shop; `?view=demo` forces demo; anonymous gets demo | no (safe method) |
| GET | `/api/credit/explain` | demo-read | session shop; `?view=demo` forces demo; anonymous gets demo | no (safe method) |
| POST | `/api/passports` | login required | snapshot built from session shop only | yes |
| GET | `/api/passports` | login required | lists session shop only | no (safe method) |
| POST | `/api/passports/{id}/revoke` | login required | id must belong to session shop, else 404 | yes |
| GET | `/api/passports/{id}/activity` | login required | id must belong to session shop, else 404 | no (safe method) |
| GET | `/api/p/{token}` | public | shop taken from the passport row itself | no (safe method) |
| GET | `/api/health` | public | none (liveness probe) | no |
| GET | `/{path:path}` | public | none (static frontend bundle / SPA fallback) | no |

Notes:

- Cross-shop access (transaction, draft, or passport ids belonging to
  another shop) returns 404, indistinguishable from a missing id.
- Anonymous demo reads serve the shop with `shops.is_demo = 1`; without
  that row they return 404. Anonymous visitors can never write anything.
- State-changing requests (all POSTs) verify the `Origin` header matches
  our own origin or the `DUKA_ALLOWED_ORIGINS` list.
- `audit_log` rows written by scoped endpoints carry the session
  `shop_id`; the public lender-view audit row keeps `shop_id` NULL.
- Explain and transcribe calls consume both the global daily cap
  (`DUKA_DAILY_AI_CAP`, default 400) and the per-shop quota
  (`DUKA_SHOP_AI_CAP`, default 60); when either is spent, explanations
  fall back to the template exactly as before.
