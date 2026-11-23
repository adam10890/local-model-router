# 0008 - Cloud Imperium serves a separate app later

## Status

Accepted

## Context

Imperium should be installable as a standalone gateway on a cloud server.
Hermes may not run on that host. The primary cloud client is not decided as
Hermes-on-server.

## Decision

We will treat the cloud Imperium client as a separate application the founder builds later, and we will not assume Hermes is that UI.

The main client for a cloud Imperium install is a **separate application the
founder builds**. Adaptations for that app are created separately later.

Do not assume Hermes is the cloud-side UI. Hermes remains the primary
workstation client.

## Consequences

- Cloud install docs describe Imperium as a compute gateway, not a Hermes
  host.
- Cloud client contract stays TBD until that app exists.
- Workstation Hermes priority (ADR 0001) is unchanged.

Negative: the cloud client contract stays undecided, so a cloud install cannot yet target a real client.
