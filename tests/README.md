# Tests

Unit/integration tests for the manager. No docker or network required.

```bash
python -m pip install "fastapi" "uvicorn[standard]" "httpx"
python tests/test_app.py      # auth gate, CSRF, key masking, standby API
python tests/test_phase.py    # live -> standby -> closed -> auto-resume machine
python tests/test_streams.py  # stale SRS entries / dead-publisher detection
```

Both exit non-zero on failure, so they work in CI.

What they cover:

- **test_app** — unauthenticated requests are rejected, `/` redirects to
  `/login`, bad passwords fail, CSRF token is required on mutations,
  **stream keys never appear in any API response**, standby upload rejects
  non-images and accepts real ones, logout invalidates the session.
- **test_phase** — the drop state machine: OBS up => live; OBS gone => standby
  with a live countdown; past `STANDBY_SECONDS` => closed; OBS returning later
  => automatic resume; `STANDBY_SECONDS=0` => immediate close; no standby image
  => close rather than feed a dead source.
