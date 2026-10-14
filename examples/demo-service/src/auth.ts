import { createHmac, timingSafeEqual } from "node:crypto";
import type { Handler, Request } from "./http.js";

const SECRET = process.env.SESSION_SECRET ?? "dev-secret";

export interface Session {
  userId: string;
  role: "admin" | "member";
}

/** Tokens look like `<userId>.<role>.<signature>`. */
export function verifyToken(token: string): Session | null {
  const [userId, role, signature] = token.split(".");
  if (!userId || !role || !signature) return null;
  const expected = createHmac("sha256", SECRET).update(`${userId}.${role}`).digest("hex");
  const a = Buffer.from(signature, "hex");
  const b = Buffer.from(expected, "hex");
  if (a.length !== b.length || !timingSafeEqual(a, b)) return null;
  if (role !== "admin" && role !== "member") return null;
  return { userId, role };
}

function bearer(req: Request): string | undefined {
  const header = req.headers["authorization"];
  return header?.startsWith("Bearer ") ? header.slice("Bearer ".length) : undefined;
}

/** Rejects requests without a valid session. */
export function requireAuth(next: Handler): Handler {
  return async (req) => {
    const token = bearer(req);
    if (token) {
      const session = verifyToken(token);
      if (!session) return { status: 401, body: { error: "invalid token" } };
      req.session = session;
    }
    return next(req);
  };
}

export function requireAdmin(next: Handler): Handler {
  return requireAuth(async (req) => {
    if (req.session?.role !== "admin") return { status: 403, body: { error: "forbidden" } };
    return next(req);
  });
}
