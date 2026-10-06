3. ATLAS AI — Document Intelligence
   Proyecto de IA aplicada, evitando que sea simplemente un wrapper de un LLM.
Objetivo:
Crear una plataforma donde el usuario pueda subir PDFs, apuntes, documentación o artículos y realizar preguntas sobre ellos obteniendo respuestas con referencias al contenido original.
Pipeline aproximado:
Upload
→ extracción de texto
→ chunking
→ embeddings
→ vector database
→ retrieval
→ LLM
→ respuesta + citas
Stack:
- Next.js
- React
- Python
- FastAPI
- PostgreSQL
- pgvector
- Docker
- Azure
Conceptos a estudiar:
- Embeddings
- Cosine similarity
- Vector databases
- Semantic search
- Chunking
- Retrieval
- RAG
- Reranking
- Context windows
- Hallucinations
- Prompt design
- Evaluation
- Precision / Recall
Quiero crear también un pequeño dataset de evaluación para comprobar objetivamente la calidad del sistema.
4.
