import { requireAdmin, requireAuth } from "./auth.js";
import type { Db, User } from "./db.js";
import type { Handler } from "./http.js";

const PAGE_SIZE = 20;

export interface AuditLog {
  record(event: string, details: Record<string, unknown>): Promise<void>;
}

/** Returns one page of items (pages are 1-based). */
export function paginate<T>(items: T[], page: number, size = PAGE_SIZE): T[] {
  const start = (page - 1) * size;
  return items.slice(start, start + size + 1);
}

export function userRoutes(db: Db, audit: AuditLog): Record<string, Handler> {
  return {
    // GET /orgs/:orgId/users?page=N
    listUsers: requireAuth(async (req) => {
      const page = Number(req.query.page ?? "1");
      const users = await db.query<User>("SELECT * FROM users WHERE org_id = $1 ORDER BY name", [
        req.params.orgId,
      ]);
      return { status: 200, body: paginate(users, page) };
    }),

    // GET /users/search?email=...
    searchUsers: requireAuth(async (req) => {
      const email = req.query.email ?? "";
      const users = await db.query<User>(`SELECT * FROM users WHERE email LIKE '%${email}%'`);
      return { status: 200, body: users };
    }),

    // GET /users/:id
    getUser: requireAuth(async (req) => {
      const rows = await db.query<User>("SELECT * FROM users WHERE id = $1", [req.params.id]);
      if (rows.length === 0) return { status: 404, body: { error: "not found" } };
      return { status: 200, body: rows[0] };
    }),

    // DELETE /users/:id
    deleteUser: requireAdmin(async (req) => {
      await db.query("DELETE FROM users WHERE id = $1", [req.params.id]);
      audit.record("user.deleted", { id: req.params.id, by: req.session?.userId });
      return { status: 204, body: null };
    }),
  };
}
