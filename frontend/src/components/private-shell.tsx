"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import type { ReactNode } from "react";

import { describeError } from "@/lib/api/messages";
import { useSession } from "@/lib/auth/session";

import { FileIcon, SparkIcon } from "./icons";
import { Brand } from "./logo";
import styles from "./private-shell.module.css";

export const LOGIN_PATH = "/login";

const NAV = [
  { href: "/documents", label: "Documentos", Icon: FileIcon },
  { href: "/ask", label: "Preguntar", Icon: SparkIcon },
] as const;

/**
 * Marco de las rutas privadas. Sin sesión redirige a `/login` y no pinta nada del contenido;
 * si no se pudo comprobar la sesión (red, servidor) lo dice y permite reintentar, en vez de
 * expulsar a una persona que quizá sí tiene sesión.
 */
export function PrivateShell({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const { state, signOut, refresh } = useSession();
  const [signOutError, setSignOutError] = useState<string | null>(null);
  const [signingOut, setSigningOut] = useState(false);

  useEffect(() => {
    if (state.status === "anonymous") router.replace(LOGIN_PATH);
  }, [state.status, router]);

  if (state.status === "loading" || state.status === "anonymous") {
    return (
      <p role="status" className={styles.status}>
        <span className={styles.spinner} aria-hidden="true" />
        {state.status === "loading" ? "Comprobando sesión…" : "Redirigiendo al acceso…"}
      </p>
    );
  }

  if (state.status === "error") {
    return (
      <div role="alert" className={styles.status}>
        <p>No se pudo comprobar tu sesión. Revisa tu conexión e inténtalo de nuevo.</p>
        <button type="button" className={styles.button} onClick={() => void refresh()}>
          Reintentar
        </button>
      </div>
    );
  }

  async function onSignOut() {
    if (signingOut) return;
    setSigningOut(true);
    setSignOutError(null);
    try {
      await signOut();
    } catch (error) {
      setSignOutError(describeError(error, "No se pudo cerrar la sesión."));
      setSigningOut(false);
    }
  }

  return (
    <div className={styles.shell}>
      <header className={styles.header}>
        <div className={styles.bar}>
          <Brand href="/documents" />
          <nav aria-label="Principal" className={styles.nav}>
            {NAV.map(({ href, label, Icon }) => (
              <Link
                key={href}
                href={href}
                className={styles.link}
                aria-current={pathname === href ? "page" : undefined}
              >
                <Icon size={16} />
                {label}
              </Link>
            ))}
          </nav>
          <div className={styles.user}>
            <span className={styles.avatar} aria-hidden="true">
              {state.user.email.charAt(0).toUpperCase()}
            </span>
            <span className={styles.email}>{state.user.email}</span>
            <button type="button" className={styles.button} onClick={onSignOut} disabled={signingOut}>
              {signingOut ? "Saliendo…" : "Cerrar sesión"}
            </button>
          </div>
        </div>
      </header>
      {signOutError && (
        <p role="alert" className={styles.error}>
          {signOutError}
        </p>
      )}
      <div className={styles.content}>{children}</div>
    </div>
  );
}
