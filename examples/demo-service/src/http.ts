import type { Session } from "./auth.js";

export interface Request {
  method: string;
  path: string;
  headers: Record<string, string | undefined>;
  query: Record<string, string | undefined>;
  params: Record<string, string>;
  session?: Session;
}

export interface Response {
  status: number;
  body: unknown;
}

export type Handler = (req: Request) => Promise<Response>;
