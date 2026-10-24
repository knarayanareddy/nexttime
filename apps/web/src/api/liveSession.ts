/**
 * Optional live FastAPI (/v1/health + /v1/rehearsal/ws) bridge using relative browser URLs.
 * Never hardcodes localhost so reverse-proxied Docker Compose and preview hosts work seamlessly.
 */

import type { SituationId } from "../data/scenarios";

export interface LiveHealthStatus {
  reachable: boolean;
  version: string | null;
}

export async function probeLiveBackendHealth(): Promise<LiveHealthStatus> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 1400);
  try {
    const res = await fetch("/v1/health", {
      method: "GET",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    });
    window.clearTimeout(timer);
    if (!res.ok) {
      return { reachable: false, version: null };
    }
    const payload = (await res.json()) as { status?: string; version?: string };
    if (payload.status === "ok") {
      return { reachable: true, version: payload.version ?? "0.1.0" };
    }
    return { reachable: false, version: null };
  } catch {
    window.clearTimeout(timer);
    return { reachable: false, version: null };
  }
}

export function buildRelativeRehearsalWsUrl(params: {
  situation: SituationId;
  hapticsEnabled: boolean;
  learnerId: string;
}): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const query = new URLSearchParams({
    token: `learner:${params.learnerId}`,
    situation: params.situation,
    target_device: "cli_rehearsal_simulator",
    haptics_enabled: params.hapticsEnabled ? "true" : "false",
  });
  return `${proto}//${window.location.host}/v1/rehearsal/ws?${query.toString()}`;
}

export function createSyntheticS16leChunkBase64(sampleCount = 800): string {
  const bytes = new Uint8Array(sampleCount * 2);
  const view = new DataView(bytes.buffer);
  for (let i = 0; i < sampleCount; i += 1) {
    const val = Math.round(Math.sin((i / sampleCount) * Math.PI * 6) * 2400);
    view.setInt16(i * 2, val, true);
  }
  let binary = "";
  for (let i = 0; i < bytes.length; i += 1) {
    binary += String.fromCharCode(bytes[i] ?? 0);
  }
  return btoa(binary);
}
