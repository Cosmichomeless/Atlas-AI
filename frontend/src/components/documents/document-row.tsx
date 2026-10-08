"use client";

import { useState } from "react";

import { deleteDocument, getDocument } from "@/lib/api/documents";
import type { DocumentSummary } from "@/lib/api/documents";
import { ApiError } from "@/lib/api/errors";
import { describeError } from "@/lib/api/messages";
import { STATUS_LABEL } from "@/lib/documents/status";
import { formatSize } from "@/lib/documents/validation";

import { FileIcon } from "../icons";
import styles from "./document-row.module.css";

const dateFormat = new Intl.DateTimeFormat("es", { dateStyle: "medium", timeStyle: "short" });

/** Extensión en minúsculas, para dar a cada tipo de archivo su color en el icono. */
function kindOf(filename: string): "pdf" | "md" | "txt" {
  const extension = filename.split(".").pop()?.toLowerCase();
  if (extension === "pdf") return "pdf";
  return extension === "md" || extension === "markdown" ? "md" : "txt";
}

const NO_CAUSE = "El procesamiento falló y no se registró la causa. Puedes eliminarlo y volver a subirlo.";

interface Props {
  document: DocumentSummary;
  /** Se llama cuando el documento ya no existe en el servidor (borrado o ya inexistente). */
  onDeleted: (id: string) => void;
}

export function DocumentRow({ document, onDeleted }: Props) {
  const [cause, setCause] = useState<string | null>(null);
  const [causeLoading, setCauseLoading] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const processing = document.status === "PROCESSING";
  const failed = document.status === "FAILED";

  async function showCause() {
    setCauseLoading(true);
    setError(null);
    try {
      const detail = await getDocument(document.id);
      setCause(detail.error_summary?.trim() || NO_CAUSE);
    } catch (caught) {
      setError(describeError(caught, "No se pudo cargar la causa del fallo."));
    } finally {
      setCauseLoading(false);
    }
  }

  async function remove() {
    setDeleting(true);
    setError(null);
    try {
      await deleteDocument(document.id);
      onDeleted(document.id);
    } catch (caught) {
      // 404: ya no existe (p. ej. borrado desde otra pestaña); la lista debe reflejarlo igualmente.
      if (caught instanceof ApiError && caught.status === 404) {
        onDeleted(document.id);
        return;
      }
      setError(describeError(caught, "No se pudo eliminar el documento."));
      setConfirming(false);
    } finally {
      setDeleting(false);
    }
  }

  return (
    <li className={styles.item}>
      <div className={styles.main}>
        <span className={styles.kind} data-kind={kindOf(document.filename)} aria-hidden="true">
          <FileIcon size={20} />
        </span>
        <div className={styles.text}>
          <p className={styles.name}>{document.filename}</p>
          <p className={styles.meta}>
            {formatSize(document.size_bytes)} · {dateFormat.format(new Date(document.created_at))}
          </p>
        </div>
        <span className={styles.status} data-status={document.status}>
          <span className={styles.dot} aria-hidden="true" />
          {STATUS_LABEL[document.status]}
        </span>
      </div>

      {cause && (
        <p className={styles.cause}>
          <strong>Causa del fallo:</strong> {cause}
        </p>
      )}
      {error && (
        <p role="alert" className={styles.error}>
          {error}
        </p>
      )}

      <div className={styles.actions}>
        {failed && !cause && (
          <button type="button" className={styles.action} onClick={() => void showCause()} disabled={causeLoading}>
            {causeLoading ? "Cargando…" : "Ver causa"}
          </button>
        )}
        {confirming ? (
          <>
            <span>¿Eliminar «{document.filename}»?</span>
            <button type="button" className={styles.confirm} onClick={() => void remove()} disabled={deleting}>
              {deleting ? "Eliminando…" : "Sí, eliminar"}
            </button>
            <button type="button" className={styles.action} onClick={() => setConfirming(false)} disabled={deleting}>
              Cancelar
            </button>
          </>
        ) : (
          <button
            type="button"
            className={styles.action}
            onClick={() => setConfirming(true)}
            disabled={processing}
            aria-label={`Eliminar ${document.filename}`}
            title={processing ? "No se puede eliminar mientras se procesa" : undefined}
          >
            Eliminar
          </button>
        )}
      </div>
    </li>
  );
}
