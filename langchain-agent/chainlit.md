# Agente LLA

Puedes conversar directamente con el agente. Si ya existe un índice Chroma local,
se carga automáticamente al abrir el chat.

La ingesta web es opcional. Escribe `/ingestar https://...` o pega una URL HTTPS para añadir
contexto web y revisar el proceso de extracción, fragmentación y vectorización.

Escribe `/cliente C001` para habilitar las consultas al cliente mock de SQLite y la memoria Qdrant.
Solo hay datos ficticios en esta demo.
