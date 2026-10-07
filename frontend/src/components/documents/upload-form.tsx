"use client";

import { useId, useRef, useState } from "react";
import type { FormEvent } from "react";

import type { DocumentDetail } from "@/lib/api/documents";
import { describeError } from "@/lib/api/messages";
import { uploadDocument } from "@/lib/api/upload";
import { ACCEPTED_LABEL, ACCEPT_ATTRIBUTE, MAX_UPLOAD_MB, validateFile } from "@/lib/documents/validation";

import styles from "./upload-form.module.css";

export function UploadForm({ onUploaded }: { onUploaded: (document: DocumentDetail) => void }) {
  const inputId = useId();
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [invalid, setInvalid] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const uploading = progress !== null;

  function choose(selected: File | null) {
    setDone(null);
    setFile(selected);
    setFailure(null);
    setInvalid(selected ? validateFile(selected) : null);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (uploading) return;
    if (!file) {
      setInvalid("Elige un archivo para subir.");
      return;
    }
    const problem = validateFile(file);
    if (problem) {
      setInvalid(problem);
      return;
    }
    setFailure(null);
    setDone(null);
    setProgress(0);
    try {
      const document = await uploadDocument(file, { onProgress: setProgress });
      setDone(`«${document.filename}» se ha subido y se está procesando.`);
      setFile(null);
      if (input.current) input.current.value = "";
      onUploaded(document);
    } catch (caught) {
      setFailure(describeError(caught, "No se pudo subir el archivo. Inténtalo de nuevo."));
    } finally {
      setProgress(null);
    }
  }

  const percent = Math.round((progress ?? 0) * 100);
  const error = invalid ?? failure;

  return (
    <form className={styles.form} onSubmit={submit} noValidate>
      <div className={styles.field}>
        <label htmlFor={inputId}>Subir un documento</label>
        <input
          id={inputId}
          ref={input}
          type="file"
          accept={ACCEPT_ATTRIBUTE}
          disabled={uploading}
          aria-invalid={error ? true : undefined}
          aria-describedby={`${inputId}-hint`}
          onChange={(event) => choose(event.target.files?.[0] ?? null)}
        />
        <p id={`${inputId}-hint`} className={styles.hint}>
          {ACCEPTED_LABEL}. Máximo {MAX_UPLOAD_MB} MB.
        </p>
      </div>

      {uploading && (
        <div className={styles.progress}>
          <progress value={percent} max={100} aria-label="Progreso de la subida" />
          <span>{percent} %</span>
        </div>
      )}
      {error && (
        <p role="alert" className={styles.error}>
          {error}
        </p>
      )}
      {done && (
        <p role="status" className={styles.done}>
          {done}
        </p>
      )}

      <button type="submit" className={styles.submit} disabled={uploading || !file || invalid !== null}>
        {uploading ? "Subiendo…" : "Subir"}
      </button>
    </form>
  );
}
