import Link from "next/link";

import styles from "./page.module.css";

export default function Home() {
  return (
    <main className={styles.main}>
      <p className={styles.eyebrow}>Atlas AI</p>
      <h1 className={styles.title}>Pregunta a tus documentos</h1>
      <p className={styles.lead}>
        Sube tus PDF, notas y archivos Markdown y obtén respuestas con citas a las fuentes
        originales.
      </p>
      <p className={styles.actions}>
        <Link href="/login" className={styles.primary}>
          Entrar
        </Link>
        <Link href="/register">Crear cuenta</Link>
      </p>
    </main>
  );
}
