"use client";

import { useCallback, useEffect, useState } from "react";

import { PAGE_SIZE, listDocuments } from "@/lib/api/documents";
import type { DocumentList } from "@/lib/api/documents";
import { describeError } from "@/lib/api/messages";
import { STATUS_LABEL } from "@/lib/documents/status";
import { formatSize } from "@/lib/documents/validation";

import styles from "./document-library.module.css";
import { UploadForm } from "./upload-form";

const dateFormat = new Intl.DateTimeFormat("es", { dateStyle: "medium", timeStyle: "short" });

export function DocumentLibrary() {
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<DocumentList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async (target: number) => {
    setLoading(true);
    try {
      let next = await listDocuments(target);
      // Si se borró lo último de la última página, se retrocede a la última que exista.
      if (next.items.length === 0 && next.total > 0 && target > 0) {
        target = Math.max(0, (Math.ceil(next.total / PAGE_SIZE) - 1) * PAGE_SIZE);
        next = await listDocuments(target);
      }
      setOffset(target);
      setPage(next);
      setError(null);
    } catch (caught) {
      setError(describeError(caught, "No se pudieron cargar tus documentos."));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- carga inicial desde la API
    void load(0);
  }, [load]);

  const total = page?.total ?? 0;
  const first = total === 0 ? 0 : offset + 1;
  const last = Math.min(offset + PAGE_SIZE, total);

  return (
    <section className={styles.library} aria-labelledby="documents-title">
      <h1 id="documents-title">Mis documentos</h1>

      <UploadForm onUploaded={() => void load(0)} />

      {error && (
        <div role="alert" className={styles.error}>
          <p>{error}</p>
          <button type="button" onClick={() => void load(offset)}>
            Reintentar
          </button>
        </div>
      )}

      {loading && !page && <p className={styles.muted}>Cargando documentos…</p>}

      {page && page.items.length === 0 && !error && (
        <p className={styles.empty}>Todavía no has subido ningún documento. Empieza subiendo uno arriba.</p>
      )}

      {page && page.items.length > 0 && (
        <>
          <ul className={styles.list} aria-busy={loading}>
            {page.items.map((document) => (
              <li key={document.id} className={styles.item}>
                <div>
                  <p className={styles.name}>{document.filename}</p>
                  <p className={styles.meta}>
                    {formatSize(document.size_bytes)} · {dateFormat.format(new Date(document.created_at))}
                  </p>
                </div>
                <span className={styles.status} data-status={document.status}>
                  {STATUS_LABEL[document.status]}
                </span>
              </li>
            ))}
          </ul>

          <nav className={styles.pager} aria-label="Paginación">
            <button type="button" disabled={loading || offset === 0} onClick={() => void load(Math.max(0, offset - PAGE_SIZE))}>
              Anterior
            </button>
            <span>
              {first}–{last} de {total}
            </span>
            <button type="button" disabled={loading || last >= total} onClick={() => void load(offset + PAGE_SIZE)}>
              Siguiente
            </button>
          </nav>
        </>
      )}
    </section>
  );
}
