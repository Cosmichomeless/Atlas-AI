# Criterios de anotación — atlas-qa v1.0.0

Guía para anotar, revisar o ampliar las preguntas de `questions.json`. Si cambia un criterio, sube
la versión del dataset (ver «Versionado»).

## Tipos de pregunta

| Tipo | Cuándo se usa | Comportamiento esperado del sistema |
| --- | --- | --- |
| `answerable` | El corpus contiene la respuesta completa y sin dudas. | Responder con los datos clave y citar la evidencia. |
| `ambiguous` | La pregunta, tal como está escrita, admite dos o más lecturas razonables y el corpus responde a cada una. | Responder a alguna lectura con citas correctas, o abstenerse. Nunca inventar ni mezclar lecturas. |
| `unanswerable` | El corpus no contiene la respuesta, aunque toque un tema cercano. | Abstenerse. Cualquier respuesta con contenido es una alucinación. |

## Reglas por campo

- **`question`**: en español, tal como la escribiría una persona; sin referencias al documento
  («según el texto…»). Una pregunta ambigua debe serlo de verdad: el anotador debe poder nombrar al
  menos dos lecturas con respuestas distintas.
- **`evidence`**: cita **literal** del documento (se valida automáticamente, ignorando saltos de
  línea). Debe ser la frase mínima que basta para responder. Si hacen falta varias frases, varias
  citas. Una cita no puede ser un título.
- **`key_facts`**: los datos que una respuesta correcta debe contener, tal como aparecen en la
  evidencia (se validan contra ella). La comparación en la evaluación ignora mayúsculas y acentos.
  Un dato clave por hecho; no se anotan datos que la pregunta no pide.
- **`interpretations`** (solo ambiguas): una entrada por lectura, con su `reading`, su `evidence` y
  sus `key_facts`. Las lecturas deben ser distintas entre sí.
- **`rationale`** (ambiguas y sin respuesta): una frase que justifica la anotación. En las sin
  respuesta debe decir por qué el corpus no la cubre y si el tema es cercano.

## Preguntas sin respuesta: tres variantes

Se incluyen a propósito de tres clases, porque fallan de forma distinta:

1. **Tema ausente** (`u001`, `u002`): nada en el corpus se relaciona.
2. **Tema cercano, dato ausente** (`u003`, `u004`): el corpus habla de lo mismo pero no de ese dato;
   es donde más tienta a alucinar.
3. **Fuera de dominio o temporal** (`u005`, `u006`): la pregunta no es sobre el corpus o pide un
   histórico que no existe.

## Qué no se hace

- No se anotan preguntas cuya respuesta dependa de conocimiento externo al corpus.
- No se reutilizan frases de evidencia ajenas a la pregunta para «rellenar».
- No se editan los documentos para que encajen con una pregunta sin subir la versión.

## Versionado

El dataset sigue versionado semántico, declarado en `manifest.json` junto a la huella SHA-256 de
`documents/` y `questions.json`:

- **Parche** (1.0.x): corregir erratas que no cambian el significado ni la evidencia.
- **Menor** (1.x.0): añadir preguntas o documentos sin modificar los existentes.
- **Mayor** (x.0.0): cambiar o quitar preguntas o documentos. La carpeta pasa a `atlas-qa-v<mayor>` y
  los resultados de versiones mayores distintas **no son comparables**.

La carga del dataset falla si la huella no coincide, de modo que no se puede cambiar el contenido sin
declararlo. Para sellar un cambio: `uv run python -m app.evaluation.dataset` indica la huella que
corresponde.

## Calidad de la anotación

La anotación la hizo una sola persona (el autor del repositorio, con ayuda de Claude para
redactarla) sin segundo anotador, así que no hay acuerdo entre anotadores medido. Es una limitación
conocida: las preguntas ambiguas son las más sujetas a interpretación.
