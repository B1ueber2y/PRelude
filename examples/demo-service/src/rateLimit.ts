import type { Handler } from "./http.js";

const WINDOW_MS = 60_000;
const MAX_REQUESTS = Number(process.env.RATE_LIMIT ?? 100);

const hits = new Map<string, number[]>();

/** Allows at most MAX_REQUESTS per client IP per minute. */
export function rateLimit(next: Handler): Handler {
  return async (req) => {
    const ip = req.headers["x-forwarded-for"] ?? "unknown";
    const now = Date.now();
    const recent = (hits.get(ip) ?? []).filter((t) => now - t < WINDOW_MS);
    recent.push(now);
    hits.set(ip, recent);
    if (recent.length > MAX_REQUESTS) {
      return { status: 429, body: { error: "too many requests" } };
    }
    return next(req);
  };
}
