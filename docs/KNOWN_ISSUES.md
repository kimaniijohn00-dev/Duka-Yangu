# Known issues (v2)

## Missing Origin header is not rejected (found by the real-HTTP smoke test)
- POST /api/entry, /api/transactions/confirm and /api/transcribe with NO Origin header are processed instead of returning 403, and the AI usage counters move (0 -> 4).
- A foreign Origin is correctly rejected (403).
- Smoke test result: PASS 54, FAIL 4. Unit tests: 206 passed (they do not cover this case).
- Cause not yet confirmed: the app, the smoke script, or both. Next step: run the investigation prompt, add one central Origin check, add in-process tests for missing, foreign and correct Origin.
- BLOCKER: do not deploy v2 or merge it into main until this is fixed and the smoke test shows FAIL 0.
