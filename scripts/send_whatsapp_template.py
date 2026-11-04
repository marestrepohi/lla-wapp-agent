"""Send one approved WhatsApp template and record Meta's message ID locally."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

from lla_agent.persistence.sqlite import SQLiteRepository
from lla_langchain_agent.whatsapp_opening import parse_opening_callback_data

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = PROJECT_ROOT / "langchain-agent"
load_dotenv(APP_DIR / ".env")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Envía un template aprobado de WhatsApp y guarda su WAMID en SQLite."
    )
    parser.add_argument("--to", required=True, help="Teléfono destino en formato internacional.")
    parser.add_argument(
        "--template-name",
        default="jaspers_market_order_confirmation_v1",
        help="Nombre exacto del template aprobado en Meta.",
    )
    parser.add_argument("--language", default="en_US", help="Código de idioma del template.")
    parser.add_argument(
        "--body-parameter",
        action="append",
        default=[],
        help="Valor de un parámetro de texto del body; repetir según el template.",
    )
    parser.add_argument(
        "--confirm-send",
        action="store_true",
        help="Confirma que quieres enviar el template al teléfono indicado.",
    )
    parser.add_argument(
        "--opening-context",
        help=(
            "Contexto JSON allowlisted, por ejemplo: "
            "'{\"v\":\"1\",\"flow\":\"contact_confirmation\","
            "\"contact_name\":\"Santiago\","
            "\"opening_message\":\"Hola, ¿hablo con Santiago?\"}'"
        ),
    )
    args = parser.parse_args()

    normalized_to = args.to.strip()
    if not re.fullmatch(r"\+?\d{8,15}", normalized_to):
        parser.error("--to debe llevar solo dígitos y código de país, con + inicial opcional.")
    args.to = normalized_to.lstrip("+")

    if not args.confirm_send:
        parser.error("Agrega --confirm-send para autorizar el envío al destinatario indicado.")
    return args


def main() -> int:
    args = parse_args()
    access_token = os.getenv("WHATSAPP_TOKEN", "").strip() or os.getenv(
        "WHATSAPP_ACCESS_TOKEN", ""
    ).strip()
    phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    graph_api_version = os.getenv("WHATSAPP_GRAPH_API_VERSION", "v25.0").strip()
    if not access_token or not phone_number_id:
        print(
            "Configura WHATSAPP_TOKEN y WHATSAPP_PHONE_NUMBER_ID en langchain-agent/.env.",
            file=sys.stderr,
        )
        return 2
    if not re.fullmatch(r"v\d+\.\d+", graph_api_version):
        print("WHATSAPP_GRAPH_API_VERSION debe tener el formato vNN.N.", file=sys.stderr)
        return 2

    normalized_opening_context = None
    if args.opening_context:
        opening_context = parse_opening_callback_data(args.opening_context)
        if opening_context is None:
            print("--opening-context no tiene el formato permitido.", file=sys.stderr)
            return 2
        normalized_opening_context = json.dumps(
            opening_context, ensure_ascii=False, separators=(",", ":")
        )

    template: dict[str, object] = {
        "name": args.template_name,
        "language": {"code": args.language},
    }
    if args.body_parameter:
        template["components"] = [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": value} for value in args.body_parameter
                ],
            }
        ]
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": args.to,
        "type": "template",
        "template": template,
    }
    if normalized_opening_context:
        payload["biz_opaque_callback_data"] = normalized_opening_context
    url = f"https://graph.facebook.com/{graph_api_version}/{phone_number_id}/messages"
    try:
        response = httpx.post(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            json=payload,
            timeout=20.0,
        )
    except httpx.RequestError as error:
        print(f"No se pudo conectar con Meta ({type(error).__name__}).", file=sys.stderr)
        return 1

    if not response.is_success:
        print(
            f"Meta rechazó el envío (HTTP {response.status_code}): {response.text}",
            file=sys.stderr,
        )
        return 1

    try:
        result = response.json()
        provider_message_id = result["messages"][0]["id"]
    except (ValueError, KeyError, IndexError, TypeError):
        print(
            "Meta respondió sin un ID de mensaje reconocible; revisa la respuesta de API.",
            file=sys.stderr,
        )
        return 1

    database_path = os.getenv(
        "LLA_DB_PATH", str(PROJECT_ROOT / "data" / "collections.sqlite3")
    )
    repository = SQLiteRepository(database_path)
    client = repository.get_client_by_external_id(args.to)
    client_id = client["client_id"] if client else None
    try:
        repository.record_message(
            message_id=provider_message_id,
            client_id=client_id,
            external_id=args.to,
            direction="outbound",
            body=f"[template:{args.template_name}]",
            delivery_status="accepted",
        )
        if normalized_opening_context:
            repository.record_whatsapp_opening_context(
                provider_message_id,
                args.to,
                normalized_opening_context,
            )
    except Exception as error:
        print(
            f"Meta aceptó el mensaje, pero falló el registro local ({type(error).__name__}). "
            f"Guarda el WAMID para seguimiento: {provider_message_id}",
            file=sys.stderr,
        )
        return 1

    print(
        json.dumps(
            {"accepted": True, "template": args.template_name, "wamid": provider_message_id}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
