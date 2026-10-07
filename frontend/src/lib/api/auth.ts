import type { components } from "./schema";
import { api, unwrap, unwrapEmpty } from "./client";
import { ApiError } from "./errors";

export type User = components["schemas"]["UserPublic"];

export const PASSWORD_MIN_LENGTH = 10;
export const PASSWORD_MAX_LENGTH = 128;

export function register(email: string, password: string): Promise<User> {
  return unwrap(api.POST("/api/v1/auth/register", { body: { email, password } }));
}

export function login(email: string, password: string): Promise<User> {
  return unwrap(api.POST("/api/v1/auth/login", { body: { email, password } }));
}

export function logout(): Promise<void> {
  return unwrapEmpty(api.POST("/api/v1/auth/logout"));
}

/** Usuario de la sesión actual, o `null` si no hay sesión (401). Otros fallos se propagan. */
export async function currentUser(): Promise<User | null> {
  try {
    return await unwrap(api.GET("/api/v1/auth/me"));
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return null;
    throw error;
  }
}
