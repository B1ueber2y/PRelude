export interface Db {
  query<T>(sql: string, params?: unknown[]): Promise<T[]>;
}

export interface User {
  id: string;
  email: string;
  name: string;
  orgId: string;
}
