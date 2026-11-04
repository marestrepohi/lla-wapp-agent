# Agente local con LangChain y Groq

## Estado actual de `main` — ejecución local

Esta rama ejecuta la conversación por WhatsApp y la interfaz Chainlit localmente sobre LangGraph.
Docker Compose levanta la interfaz, el webhook FastAPI y, con el perfil `whatsapp`, el túnel ngrok.
Meta llega al equipo local por la URL HTTPS del túnel. Esta guía no afirma que los servicios estén
desplegados en AWS. La variante de AWS está en la rama
[`aws-deployment`](https://github.com/marestrepohi/lla-wapp-agent/tree/aws-deployment).
El diseño local se muestra en [el diagrama de arquitectura](../docs/architecture-local.html).

En la implementación actual:

- SQLite guarda los perfiles de prueba, facturas, mensajes entrantes y salientes, eventos de entrega,
  propuestas pendientes y promesas confirmadas.
- El checkpoint SQLite de LangGraph conserva el hilo, la identidad de QA, el contexto del mensaje
  inicial y las etapas de una negociación entre mensajes.
- Groq sigue como modelo conversacional. La consulta de cuentas y el seguimiento de pagos usan reglas
  deterministas y SQLite; el RAG de páginas públicas usa Chroma, y Qdrant conserva recuerdos
  semánticos no financieros cuando está configurado.
- Audio e imágenes entrantes se normalizan con los modelos de voz y visión configurados en Groq.
- Las propuestas de pago no se registran hasta recibir una confirmación que coincide con el monto y
  la fecha pendientes.

### Qué se corrigió en el seguimiento de pagos

Mensajes como `100 para la otra semana` y `El viernes` se reconocen dentro de una negociación activa.
El estado parcial queda en el checkpoint y la respuesta evita la extracción de memoria y el RAG, que
eran el camino lento de esos mensajes. Las fechas relativas y los días de semana se resuelven con la
zona `America/Bogota`. Al procesar cada turno, el webhook registra duraciones de normalización,
runtime, envío y total sin guardar el número ni el texto en esos logs. La compactación de conversaciones
de WhatsApp tampoco hace una llamada adicional a Groq para resumir el historial.

La conversación iniciada antes de esta corrección puede recuperar el monto desde la pregunta anterior
del agente. El historial auditable se conserva en SQLite; si un turno no tiene respuesta saliente,
puede reprocesarse el mismo ID de mensaje de Meta sin crear un mensaje entrante duplicado.

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
FastEmbed/ONNX con `all-MiniLM-L6-v2` y Chroma persistido en disco. La misma implementación genera
vectores para la memoria de cliente en Qdrant. El texto, los fragmentos recuperados y las preguntas
se envían a Groq. Usa solo datos no sensibles en esta demo.

## Ejecutar

Desde la raíz del repo:

```bash
uv sync --project langchain-agent
uv run --project langchain-agent python -m lla_langchain_agent.cli --client-id C001
uv run --project langchain-agent python demo/run_demo.py
```

La carpeta `langchain-agent/.env` debe contener `GROQ_API_KEY`. El agente conversacional usa
`qwen/qwen3.8-27b` por defecto; puedes elegir otro modelo compatible con Groq mediante `GROQ_MODEL`.
La misma clave Groq se usa para transcribir audio y analizar imágenes. Para activar la memoria de
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
comando. Puedes conversar sin seleccionar un cliente. Si necesitas probar datos de cuenta, escribe
un ID directamente en la conversación; `/cliente C001` queda como atajo opcional. En QA local, un
ID que no exista genera un caso ficticio persistente en SQLite y queda asociado a esa sesión. No es
una verificación de identidad ni crea datos reales.

El runtime y el corpus público de Chroma se comparten entre las sesiones del proceso. Al seleccionar
otro cliente, el hash produce un hilo distinto. Cuando el historial supera 20 mensajes, el runtime
resume solo contexto no financiero y recorta mensajes antiguos en un límite de turno completo; el
historial auditable permanece en SQLite.

En la primera ejecución se crea `data/collections.sqlite3` con datos mock y
`langchain-agent/checkpoints.sqlite` con la memoria local. La interfaz RAG persiste la colección
de vectores en `langchain-agent/chroma/`. El webhook local de abajo usa un checkpointer separado
en `langchain-agent/whatsapp-checkpoints.sqlite`.

## Webhook local de WhatsApp con ngrok

El template aprobado puede enviarse desde Meta o con `curl`. Agrega `biz_opaque_callback_data` con
el tipo de flujo, el nombre del contacto y el texto real del mensaje. Meta devuelve ese dato en el
estado del mensaje; el webhook lo guarda y lo adjunta una sola vez a la primera respuesta entrante.
Después LangGraph conserva el contexto y la etapa de la conversación en su checkpoint del número.

El servidor local recibe el handshake GET de Meta, deduplica IDs mientras el proceso siga activo,
y manda texto, transcripciones de notas de voz y descripciones de imágenes al runtime compartido.
Whisper transcribe audio en español y Qwen analiza imágenes con la misma `GROQ_API_KEY`; las
respuestas se envían como texto. No hacen falta claves nuevas para recibir audio o imágenes.

El archivo `langchain-agent/.env` debe incluir las tres variables de WhatsApp y `GROQ_API_KEY`:

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

Instala las dependencias opcionales del webhook y arranca el servidor desde la raíz del repositorio:

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

### Ejecutar la interfaz y el webhook en Docker

Desde la raíz del repositorio, asegúrate de que `langchain-agent/.env` tenga las variables
anteriores y `GROQ_API_KEY`, requerida para el agente conversacional y el procesamiento de medios.
Luego ejecuta:

```bash
docker compose up --build
docker compose --profile whatsapp up --build
docker compose --profile whatsapp logs -f ngrok
```

Chainlit queda en `http://localhost:8000`. Con el perfil `whatsapp`, el webhook corre en el puerto
local `8001` y ngrok lo alcanza por el nombre `whatsapp-webhook` dentro de Compose. La salida de
ngrok muestra la URL HTTPS para Meta; agrega `/webhook` al final y usa como Verify Token el valor
de `WHATSAPP_VERIFY_TOKEN`. Por defecto el contenedor reutiliza
`${HOME}/.config/ngrok/ngrok.yml`; define `NGROK_CONFIG_PATH` si guardaste la configuración en otra
ruta. El volumen `agent-data` conserva SQLite, checkpoints, Chroma y la caché de embeddings. Para
borrar el estado local de Docker, ejecuta `docker compose down --volumes`.

Para iniciar el hilo desde el contenedor, usa un template aprobado cuyo texto visible efectivamente
pregunte si habla con Santiago. El texto de `--opening-context` debe coincidir con el mensaje que
recibirá la persona:

```bash
docker compose --profile whatsapp exec whatsapp-webhook \
  python /app/scripts/send_whatsapp_template.py \
  --to "+57<NUMERO_DESTINO>" \
  --template-name <TEMPLATE_APROBADO_QUE_PREGUNTA_POR_SANTIAGO> \
  --language es \
  --body-parameter "Santiago" \
  --opening-context '{"v":"1","flow":"contact_confirmation","contact_name":"Santiago","opening_message":"Hola Santiago, te escribimos de LLA para dar seguimiento a tu servicio. ¿Hablo contigo?"}' \
  --confirm-send
```

El script lee `WHATSAPP_TOKEN` y `WHATSAPP_PHONE_NUMBER_ID` del entorno del servicio, exige
`--confirm-send`, y guarda el `wamid` y el contexto inicial en SQLite. Si envías con `curl`, incluye
el mismo JSON como string en `biz_opaque_callback_data`; el webhook lo persiste cuando recibe un
estado de Meta. El template debe estar aprobado (por ejemplo, `Hola {{1}}, te escribimos de LLA para
dar seguimiento a tu servicio. ¿Hablo contigo?`, con `Santiago` como parámetro 1): el callback solo
permite correlacionar el hilo y no cambia el texto que Meta entrega.

La app de Meta también debe tener suscrito el campo `messages` y la cuenta de WhatsApp debe estar
suscrita a sus webhooks. Para enviar respuestas de audio harían falta además un proveedor de voz y
su clave; esta implementación responde en texto, igual que el modo actual del agente.

Para probar el flujo por WhatsApp o Chainlit en local, configura
`WHATSAPP_QA_ID_LINKING=true` en `langchain-agent/.env` y reinicia el servicio. La conversación sigue
el contexto del template: primero pregunta si habla con Santiago; con un “sí” pide el ID y, al
recibirlo, asocia el número y presenta el saldo y las facturas de inmediato. Si el ID no existe,
crea un expediente con servicios y facturas simulados en SQLite. Los IDs presembrados `1234`, `5678`,
`9012`, `C001`, `C002` y `C003` siguen disponibles.

El perfil y el vínculo con WhatsApp quedan guardados en SQLite. Cada número queda asociado a un
solo perfil; un ID ya vinculado a otro número no se puede reclamar desde el chat. Al presentar por
primera vez un expediente generado, el agente indica que sus importes son de prueba; no repite ese
aviso en cada turno.

Ejemplo de conversación:

```text
Agente: Hola, ¿hablo con Santiago?
Cliente: Sí, soy yo.
Agente: Gracias, Santiago. Para ubicar el expediente correcto, ¿me compartes tu ID de cliente?
Cliente: 847261
Agente: Gracias, Santiago. Ya ubiqué el registro 847261. Este expediente se generó para la prueba; sus importes son simulados. Tu saldo pendiente es PAB ... ¿Quieres revisar una fecha de pago?
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

### Propuesta de pago en varios mensajes

El flujo conserva en el checkpoint de LangGraph la etapa de la negociación y el monto o la fecha que
ya indicó el cliente. Por ejemplo:

```text
Cliente: ¿Puedo hacer un plan de pagos?
Agente: Dime qué monto puedes pagar y en qué fecha.
Cliente: 100 para la otra semana
Agente: ¿Qué día exacto te sirve para proponer el pago de PAB 100.00?
Cliente: El viernes
Agente: Puedo proponer PAB 100.00 para el 06/11/2026. Revísala y, si estás de acuerdo, responde exactamente `confirmo propuesta 100.00 2026-11-06`.
```

Los montos y las fechas se interpretan de forma determinista en este flujo; “la otra semana” pide
una fecha concreta y los días de la semana se calculan con la zona `America/Bogota`. La propuesta
completa queda pendiente en SQLite. Solo el comando de confirmación que repite el monto y la fecha
registra la promesa. Una respuesta como “sí” no realiza la escritura.

Si el agente quedó esperando la fecha antes de actualizar esta versión, el siguiente mensaje puede
recuperar el monto leyendo la pregunta anterior del hilo. Cuando el historial de WhatsApp se compacta,
el webhook no espera otra llamada a Groq para resumirlo; guarda un resumen local conservador y mantiene
el historial auditable en SQLite.

La memoria conversacional está en `langchain-agent/whatsapp-checkpoints.sqlite` y se separa por el
hilo de WhatsApp; por eso el mismo número conserva el contexto después de reiniciar el webhook. El
registro de mensajes, las facturas, los servicios y las gestiones están en `data/collections.sqlite3`.
Qdrant sigue reservado para recuerdos semánticos no financieros; el agente consulta SQLite cada vez
que necesita saldos, facturas o antecedentes de cobranza.

Esta función solo simula el proceso para QA: escribir un ID en el chat no demuestra que la persona
sea titular del perfil. Los registros presembrados y generados son ficticios. Mantén
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
- Resumen no financiero y recorte del historial enviado al modelo después de 20 mensajes; en WhatsApp
  la compactación es local para evitar una llamada adicional a Groq. SQLite conserva la auditoría completa.
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
`aws_lambda_telegram_agent`, y la interfaz Chainlit del repo `ava-whatsapp-agent-course`. FastEmbed
usa el modelo ONNX `all-MiniLM-L6-v2`, igual que la rama AWS.
