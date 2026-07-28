import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const baseUrl = (process.env.IMPERIUM_BASE_URL ?? "http://127.0.0.1:9000").replace(/\/$/, "");
const apiKey = process.env.IMPERIUM_API_KEY ?? "";

async function request(path: string, method: "GET" | "POST", body: unknown, signal: AbortSignal) {
  const response = await fetch(`${baseUrl}${path}`, {
    method,
    signal,
    headers: {
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  let payload: unknown = {};
  try {
    payload = text ? JSON.parse(text) : {};
  } catch {
    payload = { error: text.slice(0, 500) };
  }
  if (!response.ok) {
    const error = payload as { error?: string; detail?: string };
    throw new Error(`Imperium ${response.status}: ${error.error ?? error.detail ?? "request failed"}`);
  }
  return payload;
}

function result(payload: unknown) {
  return {
    content: [{ type: "text" as const, text: JSON.stringify(payload, null, 2) }],
    details: payload,
  };
}

export default function imperiumWorkPages(pi: ExtensionAPI) {
  pi.registerTool({
    name: "imperium_step_read",
    label: "Read Imperium step",
    description: "Read one assigned work step, its DOX chain, ownership, and event log.",
    parameters: Type.Object({ step_id: Type.String() }),
    async execute(_id, params, signal) {
      return result(
        await request(
          `/orchestrator/tickets/${encodeURIComponent(params.step_id)}`,
          "GET",
          undefined,
          signal,
        ),
      );
    },
  });

  pi.registerTool({
    name: "imperium_step_claim",
    label: "Claim Imperium step",
    description: "Atomically claim or renew one ready step before doing any work.",
    parameters: Type.Object({
      step_id: Type.String(),
      worker_id: Type.String(),
      lease_seconds: Type.Optional(Type.Integer({ minimum: 30, maximum: 86400 })),
    }),
    async execute(_id, params, signal) {
      return result(
        await request(
          `/orchestrator/tickets/${encodeURIComponent(params.step_id)}/claim`,
          "POST",
          { worker_id: params.worker_id, lease_seconds: params.lease_seconds ?? 900 },
          signal,
        ),
      );
    },
  });

  pi.registerTool({
    name: "imperium_step_log",
    label: "Log Imperium progress",
    description: "Append a concise progress event to the currently owned step.",
    parameters: Type.Object({
      step_id: Type.String(),
      worker_id: Type.String(),
      event: Type.Optional(Type.String()),
      detail: Type.String({ maxLength: 4096 }),
    }),
    async execute(_id, params, signal) {
      return result(
        await request(
          `/orchestrator/tickets/${encodeURIComponent(params.step_id)}/log`,
          "POST",
          {
            worker_id: params.worker_id,
            event: params.event ?? "progress",
            detail: params.detail,
          },
          signal,
        ),
      );
    },
  });

  pi.registerTool({
    name: "imperium_step_complete",
    label: "Complete Imperium step",
    description: "Complete the currently owned step with artifacts and an honest DOX note.",
    parameters: Type.Object({
      step_id: Type.String(),
      worker_id: Type.String(),
      summary: Type.String(),
      artifacts: Type.Optional(Type.Array(Type.String())),
      dox_changed: Type.Boolean(),
      dox_note: Type.String(),
    }),
    async execute(_id, params, signal) {
      return result(
        await request(
          `/orchestrator/tickets/${encodeURIComponent(params.step_id)}/complete`,
          "POST",
          {
            worker_id: params.worker_id,
            summary: params.summary,
            artifacts: params.artifacts ?? [],
            ...(params.dox_changed
              ? { dox_report: params.dox_note }
              : { dox_unchanged_reason: params.dox_note }),
          },
          signal,
        ),
      );
    },
  });

  pi.registerTool({
    name: "imperium_step_block",
    label: "Block Imperium step",
    description: "Mark the currently owned step blocked and hand it back to Hermes.",
    parameters: Type.Object({
      step_id: Type.String(),
      worker_id: Type.String(),
      reason: Type.String(),
      dox_changed: Type.Boolean(),
      dox_note: Type.String(),
      handoff_to: Type.Optional(Type.String()),
    }),
    async execute(_id, params, signal) {
      return result(
        await request(
          `/orchestrator/tickets/${encodeURIComponent(params.step_id)}/block`,
          "POST",
          {
            worker_id: params.worker_id,
            summary: params.reason,
            ...(params.dox_changed
              ? { dox_report: params.dox_note }
              : { dox_unchanged_reason: params.dox_note }),
            handoff_to: params.handoff_to ?? "hermes",
          },
          signal,
        ),
      );
    },
  });
}
