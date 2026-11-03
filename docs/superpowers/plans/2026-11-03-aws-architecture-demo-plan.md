# Plan: demo de arquitectura AWS para WhatsApp

> Plan para implementar en la rama `aws-deployment`; ejecución en línea porque los cambios están
> acoplados y el usuario pidió desarrollar esta rama.

**Objetivo:** mostrar una arquitectura del agente basada en servicios AWS con un flujo mínimo
FastAPI/Mangum → SQS → Lambda, empaquetado como imagen Docker y descrito con un diagrama distinguible
entre lo construido y los servicios objetivo.

**Arquitectura:** una plantilla SAM define API Gateway HTTP API, una Lambda ingress de imagen, SQS
FIFO con DLQ y una Lambda worker de imagen. El worker queda como punto de extensión; Aurora, Bedrock,
AgentCore Memory, Knowledge Bases, S3 Vectors y Transcribe aparecen como componentes objetivo en el
diagrama, sin aprovisionarse ni simularse en el código.

**Tech Stack:** AWS SAM/CloudFormation, Lambda Python 3.12 container images, FastAPI, Mangum, boto3,
API Gateway, SQS, CloudWatch.

**Spec:** `docs/superpowers/specs/2026-11-03-aws-architecture-demo-design.md`.

## Restricciones globales

- No migrar ni modificar el runtime del prototipo actual.
- No ejecutar `sam deploy`, crear recursos AWS ni consultar valores de secretos.
- Meta WhatsApp Cloud API es la única integración no AWS del diagrama y se muestra como dependencia externa.
- No incluir credenciales reales; Secrets Manager se referencia por ARN y los ejemplos locales usan valores ficticios.
- El worker de muestra no procesa saldos ni registra promesas; esas operaciones quedan como arquitectura futura.
- No añadir ni ejecutar pruebas. Validar solo formato, SAM y construcción local del contenedor si las herramientas están disponibles.

## Archivos y responsabilidades

- Crear `aws-demo/Dockerfile` y `aws-demo/requirements.txt` para la imagen Lambda de Python.
- Crear `aws-demo/src/aws_demo/webhook.py` para handshake GET, validación HMAC POST, extracción de mensajes y publicación en SQS.
- Crear `aws-demo/src/aws_demo/worker.py` para demostrar la recepción del evento SQS sin registrar su cuerpo.
- Crear `aws-demo/template.yaml` con API HTTP, Lambdas de imagen, SQS FIFO, DLQ, IAM mínimo, referencias a Secrets Manager y outputs.
- Crear `aws-demo/env.example.json` con secretos ficticios exclusivos para ejecución local.
- Crear `aws-demo/README.md` con construcción/ejecución/validación local y una advertencia clara antes de cualquier despliegue.
- Reescribir la especificación como documento de demo, con diagrama Mermaid, leyenda de alcance y mapa de servicios AWS objetivo.
- Añadir una referencia a la demo desde el `README.md` principal.

## Tareas

### Tarea 1: Crear el flujo serverless mínimo

- Implementar el handler FastAPI/Mangum y el worker SQS.
- Crear `template.yaml` con HTTP API, funciones de imagen, cola FIFO y DLQ.
- Configurar los grupos FIFO con un hash del remitente y deduplicación con el ID del mensaje.
- Evitar logs del cuerpo, teléfono, tokens o contenido de cobranza.
- Configurar retención breve de logs y cifrado administrado de SQS.

### Tarea 2: Documentar arquitectura completa y límites de la demo

- Dibujar Meta → API Gateway → Lambda ingress → SQS → Lambda worker.
- Mostrar como servicios AWS objetivo Aurora/Data API, Bedrock, AgentCore Memory, Knowledge Bases/S3 Vectors, S3, Transcribe y salida por cola.
- Usar líneas sólidas para recursos incluidos en `template.yaml` y líneas discontinuas para componentes ilustrativos no aprovisionados.
- Documentar por qué Lambda queda fuera de una VPC en la demo y Aurora se consultaría por Data API para evitar una NAT Gateway.
- Explicar que el flujo no ejecuta el grafo de negocio ni responde mensajes de WhatsApp todavía.

### Tarea 3: Dejar instrucciones locales sin desplegar

- Documentar `sam validate --lint`, `sam build --use-container` y `sam local start-api`.
- Explicar qué rutas se pueden inspeccionar localmente y qué llamada requiere una cola AWS existente.
- Indicar que `sam deploy --guided` crea recursos facturables y no se ejecuta en este trabajo.
- Verificar `git diff --check`, sintaxis Python con `compileall`, `sam validate --lint` y `sam build` si SAM y Docker están instalados; no ejecutar pruebas ni despliegues.

## Estado de esta ejecución

- Tareas 1 y 2 implementadas en `aws-demo/` y en la especificación de arquitectura.
- Tarea 3 documentada; `sam validate --lint`, `cfn-lint`, `compileall` y Ruff pasaron.
- No se pudo ejecutar `sam build`: esta distro WSL no tiene activa la integración con Docker Desktop.
- No se ejecutaron pruebas ni se aprovisionaron recursos AWS.
