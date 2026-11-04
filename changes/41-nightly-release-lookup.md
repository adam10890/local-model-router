# Fixed

- The managed-runtime release lookup now retries transient failures with a
  bounded backoff instead of failing on the first blip. This is the only online
  call the update path makes, and an unauthenticated `api.github.com` is capped
  at 60 requests/hour per IP — which GitHub-hosted runners share. It failed the
  nightly `CPU offline lifecycle` job twice on unchanged code (Windows
  validation runs #52 and #81) with nothing wrong locally.
- A settled answer — 401, 404, or a malformed body — is still reported on the
  first attempt; only transient failures (connection errors, timeouts, 403, 429
  and 5xx) are retried, and a rate-limit `Retry-After` beyond the backoff cap is
  ignored rather than stalling a nightly run for an hour.
- The Windows cleanroom lifecycle now prints each failing command's JSON payload
  before throwing. Previously the payload was captured into a variable and never
  shown, so a CI log carried only the generic message and the real cause had to
  be inferred from the source.

# Security

- Release lookups authenticate only when `IMPERIUM_GITHUB_TOKEN` is set, and
  deliberately **not** from the conventional `GITHUB_TOKEN`. That variable is
  commonly exported in a developer's shell for unrelated work, and attaching an
  operator's personal credential to an outbound request they never asked for
  would break the local-first posture in `GOALS.md` G4. An operator who sets
  nothing stays unauthenticated, exactly as before; CI sets the dedicated name
  on the one job that needs it.
