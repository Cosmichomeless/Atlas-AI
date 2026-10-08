"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import { PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH } from "@/lib/api/auth";
import { describeError, fieldErrors } from "@/lib/api/messages";
import { useSession } from "@/lib/auth/session";

import { Brand } from "./logo";
import styles from "./auth-form.module.css";

export const PRIVATE_HOME = "/documents";

const COPY = {
  login: {
    title: "Entrar",
    lead: "Accede para consultar tus documentos.",
    submit: "Entrar",
    pending: "Entrando…",
    alt: { text: "¿No tienes cuenta?", href: "/register", label: "Crear cuenta" },
    autoComplete: "current-password",
  },
  register: {
    title: "Crear cuenta",
    lead: "Sube tus documentos y pregunta con citas a las fuentes.",
    submit: "Crear cuenta",
    pending: "Creando cuenta…",
    alt: { text: "¿Ya tienes cuenta?", href: "/login", label: "Entrar" },
    autoComplete: "new-password",
  },
} as const;

export function AuthForm({ mode }: { mode: "login" | "register" }) {
  const copy = COPY[mode];
  const router = useRouter();
  const { state, signIn, signUp } = useSession();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [pending, setPending] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});

  // Quien ya tiene sesión no necesita ver el formulario.
  useEffect(() => {
    if (state.status === "authenticated") router.replace(PRIVATE_HOME);
  }, [state.status, router]);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending) return;
    setPending(true);
    setFormError(null);
    setErrors({});
    try {
      await (mode === "login" ? signIn(email, password) : signUp(email, password));
      router.replace(PRIVATE_HOME);
    } catch (error) {
      const byField = fieldErrors(error);
      setErrors(byField);
      if (Object.keys(byField).length === 0) setFormError(describeError(error));
      setPending(false);
    }
  }

  return (
    <main className={styles.page}>
      <div className={styles.brand}>
        <Brand href="/" />
      </div>
      <form className={styles.form} onSubmit={onSubmit} noValidate aria-labelledby="auth-title">
        <header className={styles.heading}>
          <h1 id="auth-title" className={styles.title}>
            {copy.title}
          </h1>
          <p className={styles.lead}>{copy.lead}</p>
        </header>

        <div className={styles.field}>
          <label htmlFor="email">Email</label>
          <input
            id="email"
            className={styles.input}
            type="email"
            name="email"
            autoComplete="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            aria-invalid={errors.email ? true : undefined}
            aria-describedby={errors.email ? "email-error" : undefined}
          />
          {errors.email && (
            <span id="email-error" className={styles.fieldError}>
              {errors.email}
            </span>
          )}
        </div>

        <div className={styles.field}>
          <label htmlFor="password">Contraseña</label>
          <input
            id="password"
            className={styles.input}
            type="password"
            name="password"
            autoComplete={copy.autoComplete}
            required
            minLength={mode === "register" ? PASSWORD_MIN_LENGTH : undefined}
            maxLength={PASSWORD_MAX_LENGTH}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            aria-invalid={errors.password ? true : undefined}
            aria-describedby={errors.password ? "password-error" : mode === "register" ? "password-hint" : undefined}
          />
          {mode === "register" && !errors.password && (
            <span id="password-hint" className={styles.hint}>
              Entre {PASSWORD_MIN_LENGTH} y {PASSWORD_MAX_LENGTH} caracteres.
            </span>
          )}
          {errors.password && (
            <span id="password-error" className={styles.fieldError}>
              {errors.password}
            </span>
          )}
        </div>

        {formError && (
          <p role="alert" className={styles.formError}>
            {formError}
          </p>
        )}

        <button type="submit" className={styles.submit} disabled={pending}>
          {pending ? copy.pending : copy.submit}
        </button>

        <p className={styles.alt}>
          {copy.alt.text} <Link href={copy.alt.href}>{copy.alt.label}</Link>
        </p>
      </form>
    </main>
  );
}
