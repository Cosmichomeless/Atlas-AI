"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { PAGE_SIZE, listDocuments } from "@/lib/api/documents";
import type { DocumentList } from "@/lib/api/documents";
import { describeError } from "@/lib/api/messages";
import { isPending } from "@/lib/documents/status";

import styles from "./document-library.module.css";
import { DocumentRow } from "./document-row";
import { UploadForm } from "./upload-form";

/** Cada cuánto se refresca la lista mientras haya documentos por terminar de procesar. */
export const POLL_INTERVAL_MS = 3000;

export function DocumentLibrary() {
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<DocumentList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // Solo cuenta la última petición lanzada: una respuesta antigua no pisa a una más reciente.
  const latest = useRef(0);
  const offsetRef = useRef(0);

  const load = useCallback(async (target: number, options: { silent?: boolean } = {}) => {
    const id = ++latest.current;
    if (!options.silent) setLoading(true);
    try {
      let next = await listDocuments(target);
      // Si se borró lo último de la última página, se retrocede a la última que exista.
      if (next.items.length === 0 && next.total > 0 && target > 0) {
        target = Math.max(0, (Math.ceil(next.total / PAGE_SIZE) - 1) * PAGE_SIZE);
        next = await listDocuments(target);
      }
      if (id !== latest.current) return;
      offsetRef.current = target;
      setOffset(target);
      setPage(next);
      setError(null);
    } catch (caught) {
      // Un refresco en segundo plano que falla no tapa la lista con un error: se reintenta solo.
      if (id === latest.current && !options.silent) {
        setError(describeError(caught, "No se pudieron cargar tus documentos."));
      }
    } finally {
      if (id === latest.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- carga inicial desde la API
    void load(0);
  }, [load]);

  // Mientras algún documento esté en cola o procesándose se vuelve a consultar; al llegar todos a
  // READY/FAILED `pending` pasa a false y el temporizador se cancela.
  const pending = page?.items.some((document) => isPending(document.status)) ?? false;
  useEffect(() => {
    if (!pending) return;
    const timer = setInterval(() => {
      if (!globalThis.document?.hidden) void load(offsetRef.current, { silent: true });
    }, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [pending, load]);

  // El documento desaparece al instante; la recarga posterior concilia paginación y total.
  const removed = useCallback(
    (id: string) => {
      setPage((current) =>
        current && {
          ...current,
          items: current.items.filter((document) => document.id !== id),
          total: Math.max(0, current.total - 1),
        },
      );
      void load(offsetRef.current, { silent: true });
    },
    [load],
  );

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
              <DocumentRow key={document.id} document={document} onDeleted={removed} />
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
