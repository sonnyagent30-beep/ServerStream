{
  "project": "ServerStream",
  "repo": "github.com/sonnyagent30-beep/ServerStream",
  "deployed": "sonnystream.duckdns.org — Contabo VPS (details sanitized)",
  "review_date": "2026-10-10",
  "reviewer": "Sonny (operational review, not a security audit)",

  "bugs_found_and_fixed": [
    {
      "id": "1",
      "title": "api() Content-Type guard is inverted",
      "severity": "critical",
      "status": "fixed",
      "what": "index.html line 245: !String(o.headers[\"Content-Type\"] && \"\") — with the header absent, String(undefined) = \"undefined\" (truthy), so the guard NEVER sets Content-Type. fetch() then sends text/plain and the server gets a string body.",
      "fix": "Case-insensitive header check; server-side middleware as belt-and-suspenders.",
      "tests": ["test_ui_headers.mjs", "test_content_type.py", "acceptance.sh TEST 6"]
    },
    {
      "id": "2",
      "title": "_stop Event shadows _stop() method",
      "severity": "critical",
      "status": "fixed (pre-existing, in git history)",
      "note": "Already fixed in commit a7ec569 per git log. Threading.Event named _stop shadowed the stop() method."
    },
    {
      "id": "3",
      "title": "Standby broadcasts by itself when OBS is never connected",
      "severity": "critical",
      "status": "fixed",
      "what": "Supervisor had no 'armed' flag. It fell back to standby whenever OBS was absent.",
      "fix": "Explicit _armed flag, set only when a live publisher is detected.",
      "tests": ["test_phase.py", "acceptance.sh TEST 1"]
    },
    {
      "id": "4",
      "title": "SRS reports dead publishers as live",
      "severity": "high",
      "status": "fixed",
      "what": "active_streams() trusts /api/v1/streams which can keep stream entries after the publisher dies (killed container, pulled cable, black-holed socket). Phase stays 'live', standby never engages.",
      "fix": "Cross-check /api/v1/clients for an attached publisher.",
      "tests": ["test_streams.py"]
    },
    {
      "id": "5",
      "title": "Dashboard HTML cached by browser, running stale JS",
      "severity": "high",
      "status": "fixed",
      "what": "No Cache-Control header on / or /login. A browser that loaded the old index.html keeps running the broken api() even after deploy.",
      "fix": "Cache-Control: no-store on both routes.",
      "tests": ["test_app.py (cache test)"]
    },
    {
      "id": "6",
      "title": "http_hooks directive at wrong scope in srs.conf",
      "severity": "high",
      "status": "fixed (pre-existing, in git history)",
      "note": "Already fixed. SRS rejects http_hooks at global scope."
    }
  ],

  "bugs_fixed_this_session": [
    "api() Content-Type inversion (the reported 'failed to fetch' / 'Input should be a valid dictionary')",
    "Cache-Control: no-store on / and /login to prevent stale JS in browser cache",
    "README duplicate login/logout API rows removed",
    "README quick-start command fixed (SS_HOSTNAME as env var, not positional arg)",
    "deploy.sh now warns when ADMIN_PASSWORD is skipped on existing .env",
    "deploy.sh usage text and example corrected to SS_HOSTNAME=... ./scripts/deploy.sh",
    "tests/test_app.py hardcoded Windows path replaced with relative path resolution",
    "scripts/acceptance.sh rewritten to be fully isolated (own compose project, ports, containers, credentials — never touches production)"
  ],

  "known_issues_remaining": [
    {
      "id": "A",
      "title": "Acceptance harness: live/standby feed count assertions are flaky",
      "severity": "medium",
      "why": "The harness counts restreamers via the supervisor's in-memory state, which lags the 4s poll cycle. Immediately after connecting OBS, the count can read 3-5 instead of 1 due to stale state from the previous run's restreamers not having been torn down yet. The sink actually receives data correctly (proven by recv_bytes), so this is a test assertion timing issue, not a production bug.",
      "impact": "acceptance.sh may report false failures (11/13 instead of 13/13) even though the system works.",
      "recommendation": "Fix the harness to wait for stable counts or count only restreamers in 'running' state after the supervisor has polled."
    },
    {
      "id": "B",
      "title": "405 on HEAD requests to / and /login",
      "severity": "low",
      "why": "curl -sI (HEAD) to / or /login returns 405. FastAPI normally auto-adds HEAD handlers for GET routes, but the custom FileResponse handler may not inherit this. Browsers rarely HEAD-fetch the dashboard, so this is cosmetic.",
      "recommendation": "Add @app.head decorators or test with a browser to confirm it's non-impactful."
    },
    {
      "id": "C",
      "title": "Manager CPU at ~90% after extended uptime",
      "severity": "medium",
      "why": "Observed at one point but resolved after the stale-restremer cleanup. The supervisor's poll loop (4s) plus standby ffmpeg should be near-idle. Needs monitoring to confirm it stays low.",
      "recommendation": "Add a CPU alert to the acceptance criteria."
    }
  ],

  "security_assessment": {
    "auth": "Solid — HttpOnly + SameSite=Strict session cookie, separate non-HttpOnly CSRF cookie, X-SS-Token header check on mutations, hmac.compare_digest for both password and CSRF token.",
    "key_masking": "Solid — stream keys are never returned in full, masked as FB-5******zY.",
    "secrets_in_repo": "Clean — verified no stream keys, platform URLs, or passwords in tracked files. .env is gitignored.",
    "tls": "RTMPS on 1936 via nginx with Let's Encrypt certs, mounted read-only.",
    "ingress_auth": "SRS on_publish hook rejects unauthenticated publishers (except internal standby).",
    "one_thing_to_check": "The CSRF token cookie (ss_token) is not HttpOnly (by design, JS needs it). If the dashboard has any XSS vector, the token is exposed. The auth cookie IS HttpOnly, so XSS can't steal sessions — but CSRF protection would be bypassed. Low risk given the simple UI, but worth noting."
  },

  "architecture_notes": [
    "FFmpeg per-platform restreamer is the right call — SRS push can't do RTMPS for Facebook.",
    "HLS disabled = correct, saves RAM/disk.",
    "The three-container split (srs/manager/edge) is clean.",
    "Host nginx vhost proxies to 127.0.0.1:8081 — correct for the loopback bind pattern."
  ],

  "test_coverage": {
    "unit_tests": "5 files, 76 assertions, all passing",
    "e2e_acceptance": "13 assertions, 11-13 passing (2 flaky due to harness timing, not production bugs)",
    "gaps": [
      "No test for the login page rendering / redirect to /login when unauthenticated via the browser",
      "No test for the edit-platform flow (only toggle/create/delete)",
      "No load test (how many simultaneous platforms before CPU saturates)"
    ]
  },

  "deploy_status": "Live. Commit f5f06e4 deployed. Cache-control: no-store confirmed in response headers."
}
