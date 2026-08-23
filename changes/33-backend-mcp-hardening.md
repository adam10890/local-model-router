# Backend and MCP hardening

- Added hermetic behavior tests for Docker, remote, subprocess, and MCP bridge
  failure and recovery paths.
- Refused silent backend fallback, recycled-process termination, and raw task
  text in MCP routing metadata.
- Added explicit coverage gates for the full suite and high-risk components.
