# Imperium Work Pages for Pi

This local Pi package exposes five worker tools:

- `imperium_step_read`
- `imperium_step_claim`
- `imperium_step_log`
- `imperium_step_complete`
- `imperium_step_block`

Set `IMPERIUM_BASE_URL` to the router origin; the default is
`http://127.0.0.1:9000`. Set `IMPERIUM_API_KEY` only in the environment when
the router requires bearer authentication.

Try the package without installing it:

```text
pi -e ./integrations/pi/imperium-work-pages
```

A worker must read and claim its assigned step before editing files. Repeating
the claim with the same worker ID renews the lease. A `409 ticket_claimed`
response means another worker owns the step.
