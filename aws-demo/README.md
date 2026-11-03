# Demo AWS del agente de WhatsApp

Esta carpeta construye una imagen Lambda con FastAPI/Mangum y demuestra el flujo de ingreso
asíncrono:

`Meta → API Gateway → Lambda ingress → SQS FIFO/DLQ → Lambda worker`

El worker solo valida la forma del evento y registra cuántos mensajes ficticios consumió; no ejecuta
LangGraph, no consulta clientes y no responde al usuario. Aurora, Bedrock, AgentCore Memory, S3,
Knowledge Bases/S3 Vectors y Transcribe están documentados como arquitectura objetivo en
[`docs/superpowers/specs/2026-11-03-aws-architecture-demo-design.md`](../docs/superpowers/specs/2026-11-03-aws-architecture-demo-design.md).

Meta WhatsApp Cloud API es la única integración externa necesaria. La plantilla recibe el ARN de un
secreto JSON existente en Secrets Manager y la Lambda lo lee en tiempo de ejecución; sus campos
`app_secret` y `verify_token` nunca se guardan en este repo. `env.example.json` contiene valores
ficticios, solo para la emulación local.

## Requisitos locales

- Docker
- AWS SAM CLI
- Python 3.12 para verificaciones de sintaxis
- No se requieren credenciales AWS para validar el template o construir la imagen.

## Validar y construir sin crear recursos AWS

Desde esta carpeta:

```bash
sam validate --lint --template-file template.yaml
sam build --use-container --template-file template.yaml
```

Para inspeccionar endpoints locales:

```bash
sam local start-api \
  --template-file .aws-sam/build/template.yaml \
  --env-vars env.example.json
```

Con el servidor activo, en otra terminal se pueden revisar `http://127.0.0.1:3000/health` y el
challenge GET de `/webhook`. El POST de un mensaje devuelve `503` sin una cola AWS configurada; para
publicarlo necesita una URL SQS real y credenciales AWS disponibles para SAM local. El archivo
`events/sqs-one-message.json` sirve como muestra del contrato que recibe el worker:

```bash
curl http://127.0.0.1:3000/health
curl --get http://127.0.0.1:3000/webhook \
  --data-urlencode 'hub.mode=subscribe' \
  --data-urlencode 'hub.verify_token=local-demo-only-verify-token' \
  --data-urlencode 'hub.challenge=12345'

sam local invoke QueueWorkerFunction \
  --template-file .aws-sam/build/template.yaml \
  --event events/sqs-one-message.json
```

## Recursos y costos

`template.yaml` declara API Gateway, dos Lambda de imagen, una cola FIFO, una DLQ y retención de logs
de siete días. `sam build` usa Docker localmente y no crea recursos AWS. No ejecutes
`sam deploy --guided` para una demostración local: el despliegue crea recursos facturables y requiere
un secreto previo de WhatsApp en Secrets Manager.

Al desplegar, SAM publicaría la imagen en ECR y CloudFormation aprovisionaría los recursos de la
plantilla. No se crea ninguna Aurora, modelo Bedrock, AgentCore Memory ni Knowledge Base: esos
componentes aparecen solo en el diagrama de arquitectura objetivo.

## Seguridad del ejemplo

- El webhook valida la firma `X-Hub-Signature-256` de Meta antes de leer el payload.
- SQS usa cifrado administrado por el servicio y la DLQ retiene mensajes fallidos.
- La conversación se ordena por un hash SHA-256 del remitente; se deduplica por hash del ID del mensaje.
- La deduplicación FIFO dura cinco minutos; el ledger idempotente durable y la tabla outbox del diseño
  objetivo no están implementados.
- El código no registra el cuerpo del webhook, números de teléfono, tokens ni contenido del cliente.
- Los perfiles ficticios del runtime local no se copian ni se usan en esta demo AWS.
