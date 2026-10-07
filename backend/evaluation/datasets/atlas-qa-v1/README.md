# atlas-qa v1.0.0

Dataset de preguntas y respuestas para evaluar Atlas AI en español.

| | |
| --- | --- |
| Versión | 1.0.0 (ver `manifest.json`) |
| Licencia | MIT, la del repositorio |
| Corpus | 4 documentos Markdown de una empresa **ficticia** (Nubia Logística) |
| Preguntas | 28: 16 respondibles, 6 ambiguas, 6 sin respuesta |
| Anotación | [`ANNOTATION.md`](ANNOTATION.md) |

## Uso permitido y límites

- Sirve para medir recuperación, fidelidad de las respuestas y abstención de Atlas AI, y para
  comparar configuraciones (modelo, fragmentación, reranking) sobre los mismos datos.
- Los textos son originales y sintéticos: no contienen datos personales ni material de terceros.
- Es pequeño y de un solo dominio (políticas internas). Sirve para detectar regresiones y comparar
  cambios, no para afirmar cuánto acierta el sistema con documentos reales.
- Los resultados solo son comparables entre ejecuciones de la **misma versión mayor**.

## Estructura

```
atlas-qa-v1/
├── manifest.json     versión, licencia, uso, recuentos y huella SHA-256
├── ANNOTATION.md     criterios de anotación
├── questions.json    preguntas, evidencia literal y datos clave
└── documents/        corpus
```

Se valida con `uv run python -m app.evaluation.dataset` desde `backend/`.
