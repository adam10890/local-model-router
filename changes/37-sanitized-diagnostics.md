## Added

- A protected `GET /diagnostics/report` endpoint with a stable versioned
  schema for doctor checks, readiness issues, safe slot/runtime failure codes,
  CPU/RAM/GPU telemetry, backend, active-slot count, fleet-control state, and
  auth-enabled state.
- Real dashboard diagnostics checks and an English/Hebrew
  `imperium-diagnostics-<UTC>.json` download flow.

## Changed

- `imperium doctor` and the HTTP diagnostics report now share one collector
  and the existing machine-readable doctor check codes.
- A stopped managed server now links to Open fleet controls / פתיחת בקרות
  Fleet without starting or repairing it automatically.

## Security

- Diagnostics reports are built from allowlisted operational fields and omit
  paths, credentials, request headers, environment values, URL credentials,
  prompts, responses, logs, and raw exception text. Partial collection
  failures return HTTP 200 with stable sanitized codes; invalid auth remains
  HTTP 401.
