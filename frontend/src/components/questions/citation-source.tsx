"use client";

import { useId, useRef, useState } from "react";

import { getPassage } from "@/lib/api/documents";
import type { DocumentPassage } from "@/lib/api/documents";
import { ApiError } from "@/lib/api/errors";
import { describeError } from "@/lib/api/messages";
import type { QuestionCitation } from "@/lib/api/questions";
import { describeLocation } from "@/lib/questions/location";

import styles from "./citation-source.module.css";

type Passage =
  | { kind: "closed" }
  | { kind: "loading" }
  | { kind: "open"; passage: DocumentPassage }
  | { kind: "unavailable"; message: string; retry: boolean };

const GONE = "Esta fuente ya no está disponible: el documento se eliminó o se volvió a procesar.";

/** Una cita de la respuesta; al pulsarla se carga y muestra el pasaje original que la respalda. */
export function CitationSource({ citation }: { citation: QuestionCitation }) {
  const [state, setState] = useState<Passage>({ kind: "closed" });
  const panelId = useId();
  const loading = useRef(false);

  async function load() {
    if (loading.current) return;
    loading.current = true;
    setState({ kind: "loading" });
    try {
      setState({ kind: "open", passage: await getPassage(citation.document_id, citation.chunk_id) });
    } catch (error) {
      // 404: la fuente se borró o se reindexó, no tiene sentido reintentar. Otro fallo sí puede ser pasajero.
      const gone = error instanceof ApiError && error.status === 404;
      setState({
        kind: "unavailable",
        message: gone ? GONE : describeError(error, "No se pudo cargar el pasaje."),
        retry: !gone,
      });
    } finally {
      loading.current = false;
    }
  }

  function onToggle() {
    if (state.kind === "open") setState({ kind: "closed" });
    else if (state.kind === "closed" || state.kind === "unavailable") void load();
  }

  const where = describeLocation(citation);
  const expanded = state.kind === "open";

  return (
    <div className={styles.source}>
      <button
        type="button"
        className={styles.trigger}
        aria-expanded={expanded}
        aria-controls={panelId}
        onClick={onToggle}
        disabled={state.kind === "loading"}
      >
        <span className={styles.label}>[{citation.label}]</span> {citation.filename}
        {where && <> <span className={styles.where}>{where}</span></>}
      </button>
      <div id={panelId} aria-live="polite">
        {state.kind === "loading" && <p role="status">Cargando pasaje…</p>}
        {state.kind === "open" && (
          <figure className={styles.passage}>
            <figcaption className={styles.where}>
              {state.passage.filename}
              {describeLocation(state.passage) && ` · ${describeLocation(state.passage)}`}
            </figcaption>
            <blockquote>{state.passage.text}</blockquote>
          </figure>
        )}
        {state.kind === "unavailable" && (
          <p role="alert" className={styles.unavailable}>
            {state.message} {state.retry && "Vuelve a pulsar la cita para reintentar."}
          </p>
        )}
      </div>
    </div>
  );
}
