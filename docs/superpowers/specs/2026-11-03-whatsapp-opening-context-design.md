# Contexto del template y conversación natural de cobranza

## Objetivo

Al responder a un template saliente, la persona debe continuar una conversación coherente: confirmar si es el contacto nombrado, compartir su ID de cliente y recibir un resumen breve de su expediente y saldo. El agente no debe repetir que es una demo en cada turno ni inventar datos financieros.

## Decisiones

1. **Conservar el envío manual con Meta Cloud API.** El `curl` lleva un `biz_opaque_callback_data` compacto con versión, tipo de flujo, nombre y texto de apertura. El webhook persiste ese contexto cuando llega el estado saliente que lo contiene.
2. **Consumir el contexto una sola vez y guardarlo en el hilo.** El callback se asocia al remitente y al WAMID en SQLite; el primer mensaje entrante consume el contexto y LangGraph lo conserva en el checkpoint. Los mensajes entrantes y estados existentes mantienen su deduplicación.
3. **Mantener explícita la confirmación de contacto.** Una respuesta afirmativa lleva a pedir el ID; una negativa bloquea consultas de cuenta en ese hilo. Preguntas ambiguas explican brevemente quién escribe y vuelven a confirmar. Un ID enviado en el mismo mensaje afirmativo también se acepta en QA.
4. **Presentar el caso desde SQLite.** En modo QA, el ID conocido se vincula al número; un ID nuevo crea un perfil simulado persistente. La respuesta inmediata incluye el saldo, vencimiento y desglose de facturas obtenido por `CollectionsAgent`; saldos inaccesibles no se inventan.
5. **Avisar una sola vez si el caso es simulado.** El resumen inicial identifica brevemente los importes de prueba. No se antepone un aviso repetido a futuras respuestas. El agente no afirma que el ID sea una verificación de identidad real.
6. **Mantener Groq como proveedor conversacional del prototipo local.** `GROQ_API_KEY` habilita el agente con el modelo `GROQ_MODEL` (por defecto `qwen/qwen3.8-27b`) y procesa audio e imágenes. No se requiere clave de OpenAI.

## Flujo

```mermaid
sequenceDiagram
    actor Op as Operador
    participant Meta as WhatsApp Cloud API
    participant Hook as Webhook local
    participant SQL as SQLite
    participant Graph as LangGraph
    participant LLM as Groq

    Op->>Meta: Envía template aprobado + callback_data
    Meta-->>Hook: Estado del mensaje con WAMID, destinatario y callback_data
    Hook->>SQL: Guarda contexto pendiente por WhatsApp ID
    Meta->>Hook: Cliente responde al template
    Hook->>SQL: Consume contexto pendiente una vez
    Hook->>Graph: Mensaje + contexto inicial
    Graph->>SQL: Registra mensaje y etapa del hilo
    Graph-->>Hook: Confirma contacto y pide ID
    Hook-->>Meta: Respuesta WhatsApp
    Meta-->>Op: Cliente confirma y envía ID
    Hook->>Graph: Respuesta y checkpoint persistido
    Graph->>SQL: Vincula remitente e ID QA; consulta facturas/saldo
    Graph-->>Hook: Resumen factual y próximo paso
    Hook-->>Meta: Respuesta WhatsApp
    Graph->>LLM: Preguntas abiertas con contexto del template/hilo/RAG
```

## Límites

- La identidad por número e ID que crea este modo es un mecanismo de QA, no autenticación para clientes reales.
- Los montos y servicios provienen de SQLite; Qdrant/RAG no es fuente de deuda.
- El contexto del callback está limitado a campos permitidos y se trata como dato, no como instrucciones del sistema.
- La plantilla visible debe existir y estar aprobada en Meta; el callback correlaciona el contenido real del template pero no altera el texto enviado.
- No se guardan tokens de Meta, claves de modelo ni credenciales en el callback ni en el prompt.
- La implementación local requiere `GROQ_API_KEY`; no admite selección de OpenAI.

## Aprobación

Implementación autorizada por el usuario en la conversación del 3 de noviembre de 2026.
