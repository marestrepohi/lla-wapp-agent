# Agente local con LangChain y Groq

Este prototipo combina un `StateGraph` de LangGraph con `create_agent` para las preguntas generales
y el RAG. El grafo ordena los controles de opt-out e identidad, la consulta de saldo, propuestas y
confirmaciones de pago y los escalamientos; el agente Groq conserva las tools de consulta de cliente
y búsqueda en páginas web.

CLI, Chainlit y los adaptadores locales de Telegram y WhatsApp llaman a `AgentRuntime.handle` con un
`InboundMessage`. El runtime abre un único checkpointer y compila el grafo una vez por proceso. El
`thread_id` se obtiene con SHA-256 de canal, conversación y cliente seleccionado; no expone el ID
externo ni el teléfono. `CollectionsAgent` conserva operaciones deterministas, pero ya no enruta
mensajes por su cuenta.

Los saldos y vencimientos se consultan desde `BillingTool` y `SQLiteRepository`. El modelo no puede
elegir el ID del cliente ni ejecutar escrituras financieras: el grafo registra una promesa solo
después de una propuesta pendiente y una confirmación que repite exactamente su monto y fecha.
El RAG usa páginas públicas,
`RecursiveCharacterTextSplitter` (1000 caracteres, 200 de solapamiento), embeddings locales
`all-MiniLM-L6-v2` y Chroma persistido en disco. El texto, los fragmentos recuperados y las preguntas
se envían a Groq. Usa solo datos no sensibles en esta demo.

## Ejecutar

Desde la raíz del repo:

```bash
uv sync --project langchain-agent
uv run --project langchain-agent python -m lla_langchain_agent.cli --client-id C001
uv run --project langchain-agent python demo/run_demo.py
```

La carpeta `langchain-agent/.env` debe contener `GROQ_API_KEY`. El modelo predeterminado es
`qwen/qwen3.8-27b`; se puede cambiar con `GROQ_MODEL` o `--model`. Para activar la memoria de
largo plazo añade `QDRANT_URL`, `QDRANT_API_KEY` y opcionalmente `QDRANT_MEMORY_COLLECTION` al mismo
archivo. Usa una clave Qdrant nueva si la anterior se compartió en un chat.

```dotenv
QDRANT_URL=https://your-cluster.aws.cloud.qdrant.io:6333
QDRANT_API_KEY=your_rotated_key
QDRANT_MEMORY_COLLECTION=lla_customer_memories
```

## Interfaz RAG con Chainlit

Desde la raíz del repo, inicia la interfaz:

```bash
uv sync --project langchain-agent
cd langchain-agent
uv run chainlit run chainlit_app.py --headless --host 127.0.0.1 --port 8000
```

Abre `http://127.0.0.1:8000` y conversa directamente con el agente. Si ya hay una colección Chroma
local, se carga automáticamente al abrir el chat. La ingesta de nuevas páginas es opcional:

```text
/ingestar https://docs.langchain.com/oss/python/langchain/overview
```

Cuando ingestas una URL, la interfaz enseña la extracción y limpieza del HTML, los fragmentos,
una muestra del vector y las fuentes recuperadas. Admite hasta cinco URLs HTTPS públicas en un
comando. Escribe `/cliente C001` para seleccionar el cliente mock de la conversación; esto solo
simula el contexto confiable del canal y no es una verificación real de identidad. Las preguntas
generales y el RAG también funcionan sin seleccionar un cliente.

El runtime y el corpus público de Chroma se comparten entre las sesiones del proceso. Al seleccionar
otro cliente, el hash produce un hilo distinto. Cuando el historial supera 20 mensajes, el runtime
resume solo contexto no financiero y recorta mensajes antiguos en un límite de turno completo; el
historial auditable permanece en SQLite.

En la primera ejecución se crea `data/collections.sqlite3` con datos mock y
`langchain-agent/checkpoints.sqlite` con la memoria local. La interfaz RAG persiste la colección
de vectores en `langchain-agent/chroma/`. El webhook local de abajo usa un checkpointer separado
en `langchain-agent/whatsapp-checkpoints.sqlite`.

## Webhook local de WhatsApp con ngrok

El servidor local recibe el handshake GET de Meta, deduplica IDs mientras el proceso siga activo,
y manda texto, transcripciones de notas de voz y descripciones de imágenes al runtime compartido.
Whisper transcribe audio en español y Qwen analiza imágenes con la misma `GROQ_API_KEY`; las
respuestas se envían como texto. No hacen falta claves nuevas para recibir audio o imágenes.

El archivo `langchain-agent/.env` debe tener las tres variables de WhatsApp que ya configuraste y
la clave Groq:

```dotenv
WHATSAPP_PHONE_NUMBER_ID=...
WHATSAPP_TOKEN=...
WHATSAPP_VERIFY_TOKEN=...
GROQ_API_KEY=...
```

Los modelos predeterminados son `whisper-large-v3-turbo` para audio y `qwen/qwen3.8-27b` para
imágenes. Puedes cambiarlos con `GROQ_AUDIO_MODEL`, `GROQ_AUDIO_LANGUAGE` (por defecto `es`) y
`GROQ_VISION_MODEL`. Se aceptan audios de hasta 25 MB e imágenes de hasta 14 MB. Esta prueba sigue
el webhook de AVA y no valida la firma de los POST; úsala solo durante la prueba local por ngrok,
no como despliegue público.

Instala la extensión web y arranca el servidor en la raíz del repo:

```bash
uv sync --project langchain-agent --extra webhook
uv run --project langchain-agent --extra webhook uvicorn lla_langchain_agent.whatsapp_webhook:app --host 0.0.0.0 --port 8001
```

En otra terminal WSL inicia el túnel:

```bash
ngrok http 8001
```

En Meta configura la Callback URL como `https://<dominio-ngrok>/webhook` y copia exactamente el
valor generado para `WHATSAPP_VERIFY_TOKEN` en Verify Token. Para que el agente conteste por Cloud
API, configura `WHATSAPP_TOKEN` y `WHATSAPP_PHONE_NUMBER_ID`.

La app de Meta también debe tener suscrito el campo `messages` y la cuenta de WhatsApp debe estar
suscrita a sus webhooks. Para enviar respuestas de audio harían falta además un proveedor de voz y
su clave; esta implementación responde en texto, igual que el modo actual del agente.

Para probar perfiles de cliente por WhatsApp en local, configura `WHATSAPP_QA_ID_LINKING=true` en
`langchain-agent/.env` y reinicia el webhook. Cuando una consulta necesite un perfil, el agente
pedirá el ID. Puedes responder `1234`, `5678`, `9012`, `C001`, `C002` o `C003`; también puedes
enviar directamente una frase como `mi ID es 1234`. SQLite busca el ID y guarda la asociación entre
ese perfil ficticio y el remitente de WhatsApp. En los mensajes siguientes, el agente resolverá el
mismo perfil automáticamente. Cada WhatsApp queda asociado a un único perfil; para cambiarlo en QA,
borra localmente su fila de `client_external_identities` en la base SQLite y vuelve a vincularlo.

Ejemplo de conversación:

```text
Cliente: ¿Cuál es mi saldo?
Agente: Para consultar un perfil en esta demo, dime tu ID de cliente...
Cliente: 1234
Agente: Listo. En esta demo asocié este WhatsApp al perfil de prueba 1234...
Cliente: ¿Cuál es mi saldo?
Agente: Tu saldo pendiente es PAB 196.00. El desglose por servicio es...
```

Para ensayar otro perfil con el mismo WhatsApp, elimina solo su asociación local en
`data/collections.sqlite3` y vuelve a escribir, por ejemplo, `mi ID es 5678`. No hace falta borrar
los perfiles de prueba.

### Expedientes ficticios de cobranza en SQLite

Al iniciar el webhook, el repositorio crea las tablas `client_services`, `invoices` y
`collection_events` y carga estos casos de prueba:

| ID QA | Servicios | Caso de cobranza | Saldo abierto inicial |
|---|---|---|---:|
| `1234` | Móvil Conecta 35 GB; Fibra Hogar 600 Mbps | Cuatro facturas vencidas y preferencia de recibir recordatorios después de las 17:00 | PAB 196.00 |
| `5678` | Fibra Hogar 750 Mbps; TV Familiar HD | Tres facturas vencidas y una promesa anterior incumplida | PAB 178.00 |
| `9012` | Móvil Control 20 GB; Internet Hogar 400 Mbps | Factura móvil vencida y factura de internet con pago parcial de PAB 20.00 | PAB 63.00 |

El saldo se calcula con las facturas abiertas (`monto - pago aplicado`); las facturas pagadas no se
suman. Pregunta por el saldo para ver el total y el desglose por plan. Pregunta, por ejemplo,
`¿qué facturas tengo?`, `¿qué plan de internet tengo?` o `¿qué pasó con mi promesa anterior?` para que
el agente consulte en SQLite los servicios, períodos, vencimientos y gestiones del expediente.
Estos datos son ficticios y se cargan con `INSERT OR IGNORE`, así que reiniciar el webhook no duplica
las filas.

La memoria conversacional está en `langchain-agent/whatsapp-checkpoints.sqlite` y se separa por el
hilo de WhatsApp; por eso el mismo número conserva el contexto después de reiniciar el webhook. El
registro de mensajes, las facturas, los servicios y las gestiones están en `data/collections.sqlite3`.
Qdrant sigue reservado para recuerdos semánticos no financieros; el agente consulta SQLite cada vez
que necesita saldos, facturas o antecedentes de cobranza.

Esta función solo simula el proceso para QA: escribir un ID en el chat no demuestra que la persona
sea titular del perfil. Los clientes numéricos son registros ficticios. Mantén
`WHATSAPP_QA_ID_LINKING=false` fuera de pruebas locales. En producción, la verificación debe enviar
un código de un solo uso al teléfono que ya figure en el sistema del cliente, validar vigencia e
intentos y asociar el `wa_id` solo después de una comprobación exitosa.

La demo no tiene servicio OTP ni acceso a un registro de teléfonos verificados. La deduplicación
actual es local al proceso; no sustituye el almacenamiento durable requerido en AWS.

## Telegram opcional

El mismo runtime puede atender un bot de Telegram durante una prueba local. Configura
`TELEGRAM_BOT_TOKEN` en el entorno y ejecuta:

```bash
uv sync --project langchain-agent --extra telegram
uv run --project langchain-agent python -m lla_langchain_agent.telegram
```

El adaptador nunca obtiene un `client_id` del texto. Para habilitar consultas de cuenta, el
repositorio debe tener un vínculo de identidad confiable para el ID externo del usuario. Sin ese
vínculo, el agente pide validar identidad y no revela saldos.

## Alcance de esta etapa

- Grafo LangGraph para opt-out, identidad, saldo, negociación, confirmación y escalamiento.
- Consulta mock de perfil y saldo; el modelo no elige el cliente.
- Escritura mock de promesa solo después de confirmar una propuesta pendiente.
- RAG con páginas web, chunking, embeddings locales, Chroma, recuperación y fuentes visibles.
- Historial conversacional persistente en el mismo `thread_id`.
- Runtime y checkpointer compartidos durante todo el proceso de cada interfaz.
- Resumen no financiero y recorte del historial enviado al modelo después de 20 mensajes; SQLite
  conserva la auditoría completa.
- Memoria semántica Qdrant entre conversaciones, ligada al `client_id` validado.
- Webhook local para Meta, túnel ngrok, runtime compartido y envío opcional por Cloud API; esta ruta local no valida firmas POST.
- Sin integración ni despliegue en AWS. Las escrituras de promesa son simuladas en SQLite y requieren confirmación explícita.

El historial de cada conversación queda en SQLite (memoria de corto plazo). La memoria semántica
entre conversaciones se guarda en Qdrant Cloud y se filtra por el `client_id` validado; requiere
`QDRANT_URL` y `QDRANT_API_KEY` en `langchain-agent/.env`. Se guardan preferencias, contexto general
y seguimientos explícitos, no transcripciones completas, saldos, deudas, credenciales ni datos de
tarjetas. En la ruta general, el nodo de memoria analiza el mensaje y recupera hasta tres recuerdos
relevantes del mismo cliente antes de invocar el agente; las rutas de cobranza no consultan ni
actualizan memoria semántica. Las páginas públicas siguen en su colección Chroma separada. Este
análisis añade una llamada al modelo por turno cuando Qdrant está activo.

Chroma se reserva para páginas web públicas compartidas por el proceso. Ni Chroma ni Qdrant
autorizan ni determinan acciones financieras: saldo, promesas y escalaciones siguen las tools y
reglas deterministas basadas en SQLite.

La estructura toma el `chunk_size=1000` / `chunk_overlap=200` del indexador del repo
`aws_lambda_telegram_agent`, y la interfaz Chainlit más el embedding local `all-MiniLM-L6-v2` del
repo `ava-whatsapp-agent-course`.
