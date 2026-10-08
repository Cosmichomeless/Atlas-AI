import Link from "next/link";

import { ArrowRightIcon, LayersIcon, QuoteIcon, ShieldIcon, UploadIcon } from "@/components/icons";
import { Brand } from "@/components/logo";

import styles from "./page.module.css";

const FEATURES = [
  {
    Icon: QuoteIcon,
    title: "Respuestas con citas",
    text: "Cada afirmación enlaza al pasaje exacto del documento del que sale. Pulsa una fuente y léela tal cual.",
  },
  {
    Icon: ShieldIcon,
    title: "Si no lo sabe, lo dice",
    text: "Cuando tus documentos no contienen la respuesta, Atlas AI se abstiene en lugar de inventarla.",
  },
  {
    Icon: LayersIcon,
    title: "Solo tus documentos",
    text: "Las respuestas se construyen con fragmentos recuperados de lo que tú subes, y nadie más puede verlos.",
  },
] as const;

const STEPS = [
  { title: "Sube", text: "PDF, texto o Markdown de hasta 20 MB. Se procesan en segundo plano." },
  { title: "Pregunta", text: "Haz una pregunta en lenguaje natural sobre todos tus documentos o solo algunos." },
  { title: "Verifica", text: "Abre cada cita y comprueba el pasaje original antes de fiarte de la respuesta." },
] as const;

export default function Home() {
  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <Brand href="/" />
        <nav aria-label="Acceso" className={styles.access}>
          <Link href="/login" className={styles.ghost}>
            Entrar
          </Link>
          <Link href="/register" className={styles.cta}>
            Crear cuenta
          </Link>
        </nav>
      </header>

      <main>
        <section className={styles.hero}>
          <div className={styles.copy}>
            <p className={styles.eyebrow}>Document Intelligence</p>
            <h1 className={styles.title}>
              Pregunta a tus <span className={styles.highlight}>documentos</span>
            </h1>
            <p className={styles.lead}>
              Sube tus PDF, notas y archivos Markdown y obtén respuestas con citas a las fuentes originales. Recuperación
              aumentada por generación, sin inventar.
            </p>
            <p className={styles.actions}>
              <Link href="/register" className={styles.primary}>
                Empezar
                <ArrowRightIcon size={18} />
              </Link>
              <Link href="/login" className={styles.secondary}>
                Ya tengo cuenta
              </Link>
            </p>
          </div>

          {/* Ilustración del producto: decorativa, no es contenido real. */}
          <div className={styles.demo} aria-hidden="true">
            <div className={styles.bubble}>¿Cuántos días por semana puedo teletrabajar?</div>
            <div className={styles.answer}>
              <p className={styles.answerLabel}>Respuesta</p>
              <p>
                Puedes teletrabajar hasta tres días por semana <span className={styles.chip}>S1</span>
              </p>
              <div className={styles.source}>
                <UploadIcon size={14} />
                <span>politica-teletrabajo.pdf · p. 2</span>
              </div>
              <blockquote className={styles.quote}>
                Cada empleada o empleado puede teletrabajar hasta tres días por semana.
              </blockquote>
            </div>
          </div>
        </section>

        <section className={styles.features} aria-labelledby="features-title">
          <h2 id="features-title" className={styles.sectionTitle}>
            Pensado para fiarte de la respuesta
          </h2>
          <ul className={styles.grid}>
            {FEATURES.map(({ Icon, title, text }) => (
              <li key={title} className={styles.feature}>
                <span className={styles.icon}>
                  <Icon size={20} />
                </span>
                <h3>{title}</h3>
                <p>{text}</p>
              </li>
            ))}
          </ul>
        </section>

        <section className={styles.steps} aria-labelledby="steps-title">
          <h2 id="steps-title" className={styles.sectionTitle}>
            Cómo funciona
          </h2>
          <ol className={styles.stepList}>
            {STEPS.map(({ title, text }, index) => (
              <li key={title} className={styles.step}>
                <span className={styles.number}>{index + 1}</span>
                <h3>{title}</h3>
                <p>{text}</p>
              </li>
            ))}
          </ol>
        </section>
      </main>

      <footer className={styles.footer}>
        <p>Atlas AI · Respuestas con citas a tus propios documentos.</p>
      </footer>
    </div>
  );
}
