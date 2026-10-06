import { api, unwrap } from "./client";

export function getHealth() {
  return unwrap(api.GET("/api/v1/health"));
}

export function getDatabaseHealth() {
  return unwrap(api.GET("/api/v1/health/db"));
}
