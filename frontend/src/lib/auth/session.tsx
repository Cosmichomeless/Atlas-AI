"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import * as authApi from "@/lib/api/auth";
import type { User } from "@/lib/api/auth";

export type SessionState =
  | { status: "loading" }
  | { status: "authenticated"; user: User }
  | { status: "anonymous" }
  | { status: "error" };

export interface Session {
  state: SessionState;
  /** Inicia sesión; lanza `ApiError` si las credenciales no son válidas. */
  signIn(email: string, password: string): Promise<void>;
  /** Crea la cuenta y abre la sesión; lanza `ApiError` si falla el registro. */
  signUp(email: string, password: string): Promise<void>;
  signOut(): Promise<void>;
  /** Vuelve a consultar al servidor (p. ej. tras un fallo de red). */
  refresh(): Promise<void>;
}

const SessionContext = createContext<Session | null>(null);

/**
 * La cookie de sesión es HttpOnly y pertenece al origen de la API: el navegador la envía solo,
 * pero este código nunca la ve. Por eso el estado se pregunta al servidor (`/auth/me`).
 */
export function SessionProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<SessionState>({ status: "loading" });

  const refresh = useCallback(async () => {
    try {
      const user = await authApi.currentUser();
      setState(user ? { status: "authenticated", user } : { status: "anonymous" });
    } catch {
      setState({ status: "error" });
    }
  }, []);

  useEffect(() => {
    // Consulta inicial al servidor: sincroniza el estado con un sistema externo (la sesión).
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void refresh();
  }, [refresh]);

  const signIn = useCallback(async (email: string, password: string) => {
    const user = await authApi.login(email, password);
    setState({ status: "authenticated", user });
  }, []);

  const signUp = useCallback(
    async (email: string, password: string) => {
      await authApi.register(email, password);
      await signIn(email, password);
    },
    [signIn],
  );

  const signOut = useCallback(async () => {
    // Si falla, la sesión sigue activa en el servidor: no se finge un cierre que no ocurrió.
    await authApi.logout();
    setState({ status: "anonymous" });
  }, []);

  const value = useMemo<Session>(
    () => ({ state, signIn, signUp, signOut, refresh }),
    [state, signIn, signUp, signOut, refresh],
  );

  return <SessionContext value={value}>{children}</SessionContext>;
}

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error("useSession debe usarse dentro de <SessionProvider>.");
  return session;
}
