"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";

import { listReadyDocuments } from "@/lib/api/documents";
import type { DocumentSummary } from "@/lib/api/documents";
import { describeError } from "@/lib/api/messages";
import { askQuestion } from "@/lib/api/questions";
import type { QuestionResponse } from "@/lib/api/questions";
import {
  ABSTENTION_FALLBACK,
  ABSTENTION_MESSAGE,
  QUESTION_MAX_CHARS,
  describeQuestionError,
} from "@/lib/questions/messages";

import styles from "./question-view.module.css";

type Outcome = { kind: "result"; value: QuestionResponse } | { kind: "error"; message: string };

/** Formulario de preguntas: elige documentos listos, pregunta y muestra respuesta, abstención o error. */
export function QuestionView() {
  const [documents, setDocuments] = useState<DocumentSummary[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  // `asking` llega tarde a un segundo envío inmediato; la ref lo corta en el mismo instante.
  const inFlight = useRef(false);

  useEffect(() => {
    let active = true;
    listReadyDocuments().then(
      (page) => {
        if (active) setDocuments(page.items);
      },
      (error: unknown) => {
        if (active) setLoadError(describeError(error, "No se pudieron cargar tus documentos."));
      },
    );
    return () => {
      active = false;
    };
  }, []);

  function toggle(id: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = question.trim();
    if (inFlight.current || text === "") return;
    inFlight.current = true;
    setAsking(true);
    setOutcome(null);
    try {
      const value = await askQuestion(text, [...selected]);
      setOutcome({ kind: "result", value });
    } catch (error) {
      setOutcome({ kind: "error", message: describeQuestionError(error) });
    } finally {
      inFlight.current = false;
      setAsking(false);
    }
  }

  const hasDocuments = documents !== null && documents.length > 0;

  return (
    <section className={styles.view} aria-labelledby="ask-title">
      <h1 id="ask-title" className={styles.title}>
        Preguntar a tus documentos
      </h1>

      {loadError && (
        <p role="alert" className={styles.error}>
          {loadError}
        </p>
      )}
      {documents !== null && documents.length === 0 && (
        <p className={styles.muted}>
          Aún no tienes documentos listos. <Link href="/documents">Sube uno</Link> y espera a que termine de procesarse.
        </p>
      )}

      <form className={styles.form} onSubmit={onSubmit}>
        {hasDocuments && (
          <fieldset className={styles.scope}>
            <legend>Documentos</legend>
            <p className={styles.muted}>
              {selected.size === 0
                ? "Sin selección, se consultan todos tus documentos listos."
                : `${selected.size} seleccionado${selected.size === 1 ? "" : "s"}.`}
            </p>
            {documents.map((document) => (
              <label key={document.id} className={styles.option}>
                <input
                  type="checkbox"
                  checked={selected.has(document.id)}
                  onChange={() => toggle(document.id)}
                  disabled={asking}
                />
                {document.filename}
              </label>
            ))}
          </fieldset>
        )}

        <label className={styles.field}>
          Tu pregunta
          <textarea
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            maxLength={QUESTION_MAX_CHARS}
            required
            disabled={asking}
          />
          <span className={styles.muted}>
            {question.length}/{QUESTION_MAX_CHARS}
          </span>
        </label>

        <button type="submit" className={styles.submit} disabled={asking || question.trim() === ""}>
          {asking ? "Buscando respuesta…" : "Preguntar"}
        </button>
      </form>

      <div aria-live="polite">
        {asking && (
          <p role="status" className={styles.muted}>
            Consultando tus documentos…
          </p>
        )}
        {outcome?.kind === "error" && (
          <div role="alert" className={`${styles.result} ${styles.error}`}>
            <p>{outcome.message}</p>
          </div>
        )}
        {outcome?.kind === "result" && <Result response={outcome.value} />}
      </div>
    </section>
  );
}

function Result({ response }: { response: QuestionResponse }) {
  if (response.status === "abstained" || response.text === null) {
    const reason = response.abstention_reason;
    return (
      <article className={`${styles.result} ${styles.abstained}`} aria-label="Sin respuesta">
        <h2>No hay respuesta</h2>
        <p>{reason ? ABSTENTION_MESSAGE[reason] : ABSTENTION_FALLBACK}</p>
      </article>
    );
  }

  return (
    <article className={styles.result} aria-label="Respuesta">
      <h2>Respuesta</h2>
      <p className={styles.answer}>{response.text}</p>
      {response.truncated && (
        <p role="note" className={styles.notice}>
          La respuesta se cortó por su longitud máxima: puede estar incompleta.
        </p>
      )}
      {response.uncited_statements.length > 0 && (
        <div className={styles.notice}>
          <p>Estas frases no tienen fuente en tus documentos:</p>
          <ul>
            {response.uncited_statements.map((statement) => (
              <li key={statement}>{statement}</li>
            ))}
          </ul>
        </div>
      )}
      {response.citations.length > 0 && (
        <>
          <h3>Fuentes</h3>
          <ul className={styles.sources}>
            {response.citations.map((citation) => (
              <li key={citation.chunk_id}>
                <span className={styles.label}>[{citation.label}]</span>
                {citation.filename}
                {citation.page !== null && `, p. ${citation.page}`}
                {citation.section && `, ${citation.section}`}
              </li>
            ))}
          </ul>
        </>
      )}
    </article>
  );
}
