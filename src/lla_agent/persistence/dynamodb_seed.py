"""Synthetic collection profiles used only by the explicit AWS QA seed command."""

from __future__ import annotations

from typing import Any


def demo_case_items() -> list[dict[str, Any]]:
    """Return deterministic service, invoice, and event rows for the QA profiles."""
    items: list[dict[str, Any]] = []
    profiles = [
        ("1234", "Cliente QA 1234", 12500, "2026-11-15", True),
        ("5678", "Cliente QA 5678", 45000, "2026-11-20", True),
        ("9012", "Cliente QA 9012", 8300, "2026-11-25", True),
        ("C001", "Ana Torres", 125000, "2026-11-15", True),
        ("C002", "Luis Pérez", 45000, "2026-11-10", True),
        ("C003", "María Gómez", 83000, "2026-11-05", True),
        ("CERR", "Cliente Prueba", 0, "2026-11-01", False),
    ]
    for client_id, full_name, balance_cents, due_date, billing_available in profiles:
        items.append(
            {
                "pk": f"CLIENT#{client_id}",
                "sk": "PROFILE",
                "item_type": "profile",
                "client_id": client_id,
                "full_name": full_name,
                "balance_cents": balance_cents,
                "currency": "PAB",
                "due_date": due_date,
                "status": "overdue",
                "billing_available": billing_available,
            }
        )

    services = [
        ("1234", "SVC-QA1234-MOB", "mobile", "Móvil Conecta 35 GB", 3900),
        ("1234", "SVC-QA1234-NET", "internet", "Fibra Hogar 600 Mbps", 5900),
        ("5678", "SVC-QA5678-NET", "internet", "Fibra Hogar 750 Mbps", 7500),
        ("5678", "SVC-QA5678-TV", "tv", "TV Familiar HD", 2800),
        ("9012", "SVC-QA9012-MOB", "mobile", "Móvil Control 20 GB", 3500),
        ("9012", "SVC-QA9012-NET", "internet", "Internet Hogar 400 Mbps", 4800),
    ]
    for client_id, service_id, service_type, plan_name, monthly_fee_cents in services:
        items.append(
            {
                "pk": f"CLIENT#{client_id}",
                "sk": f"SERVICE#{service_id}",
                "item_type": "service",
                "client_id": client_id,
                "service_id": service_id,
                "service_type": service_type,
                "plan_name": plan_name,
                "monthly_fee_cents": monthly_fee_cents,
                "status": "active",
            }
        )

    invoices = [
        ("1234", "INV-QA1234-MOB-202608", "SVC-QA1234-MOB", "2026-08", "2026-08-01", "2026-09-10", 3900, 0, "unpaid"),
        ("1234", "INV-QA1234-MOB-202609", "SVC-QA1234-MOB", "2026-09", "2026-09-01", "2026-10-10", 3900, 0, "unpaid"),
        ("1234", "INV-QA1234-NET-202608", "SVC-QA1234-NET", "2026-08", "2026-08-01", "2026-09-12", 5900, 0, "unpaid"),
        ("1234", "INV-QA1234-NET-202609", "SVC-QA1234-NET", "2026-09", "2026-09-01", "2026-10-12", 5900, 0, "unpaid"),
        ("5678", "INV-QA5678-NET-202608", "SVC-QA5678-NET", "2026-08", "2026-08-01", "2026-09-08", 7500, 0, "unpaid"),
        ("5678", "INV-QA5678-NET-202609", "SVC-QA5678-NET", "2026-09", "2026-09-01", "2026-10-08", 7500, 0, "unpaid"),
        ("5678", "INV-QA5678-TV-202609", "SVC-QA5678-TV", "2026-09", "2026-09-01", "2026-10-12", 2800, 0, "unpaid"),
        ("9012", "INV-QA9012-MOB-202609", "SVC-QA9012-MOB", "2026-09", "2026-09-01", "2026-10-08", 3500, 0, "unpaid"),
        ("9012", "INV-QA9012-NET-202608", "SVC-QA9012-NET", "2026-08", "2026-08-01", "2026-09-10", 4800, 4800, "paid"),
        ("9012", "INV-QA9012-NET-202609", "SVC-QA9012-NET", "2026-09", "2026-09-01", "2026-10-10", 4800, 2000, "partial"),
    ]
    for client_id, invoice_id, service_id, period, issue_date, due_date, amount, paid, status in invoices:
        items.append(
            {
                "pk": f"CLIENT#{client_id}",
                "sk": f"INVOICE#{due_date}#{invoice_id}",
                "item_type": "invoice",
                "client_id": client_id,
                "invoice_id": invoice_id,
                "service_id": service_id,
                "billing_period": period,
                "issue_date": issue_date,
                "due_date": due_date,
                "amount_cents": amount,
                "paid_cents": paid,
                "status": status,
            }
        )

    events = [
        ("1234", "EV-QA1234-01", "2026-10-14T15:30:00+00:00", "reminder", "Se envió un recordatorio por las facturas vencidas de telefonía e internet.", "sin respuesta"),
        ("1234", "EV-QA1234-02", "2026-10-20T16:15:00+00:00", "customer_contact", "La persona pidió recibir recordatorios después de las 17:00.", "preferencia registrada en este expediente de prueba"),
        ("1234", "EV-QA1234-03", "2026-10-25T14:00:00+00:00", "call_attempt", "Se intentó contactar para revisar el saldo vencido.", "no se logró establecer contacto"),
        ("5678", "EV-QA5678-01", "2026-10-12T15:00:00+00:00", "payment_promise", "Se acordó un pago de PAB 100.00 para el 20/10/2026.", "promesa incumplida; no aparece pago aplicado"),
        ("5678", "EV-QA5678-02", "2026-10-24T17:30:00+00:00", "customer_contact", "La persona indicó que no pudo completar el pago acordado y pidió revisar el detalle de internet y TV.", "pendiente de seguimiento"),
        ("5678", "EV-QA5678-03", "2026-11-01T14:00:00+00:00", "reminder", "Se envió un recordatorio por el saldo vencido de los servicios del hogar.", "entregado; sin respuesta registrada"),
        ("9012", "EV-QA9012-01", "2026-10-20T16:00:00+00:00", "reminder", "Se notificó el saldo pendiente de telefonía e internet.", "entregado"),
        ("9012", "EV-QA9012-02", "2026-10-28T13:45:00+00:00", "payment", "Se recibió un pago parcial de PAB 20.00 y se aplicó a la factura de internet de septiembre.", "saldo restante de esa factura: PAB 28.00"),
        ("9012", "EV-QA9012-03", "2026-10-29T15:15:00+00:00", "customer_contact", "La persona pidió confirmar cuánto queda pendiente después del pago parcial.", "requiere respuesta con saldo actualizado"),
    ]
    for client_id, event_id, occurred_at, event_type, summary, outcome in events:
        items.append(
            {
                "pk": f"CLIENT#{client_id}",
                "sk": f"EVENT#{occurred_at}#{event_id}",
                "item_type": "event",
                "client_id": client_id,
                "event_id": event_id,
                "occurred_at": occurred_at,
                "event_type": event_type,
                "summary": summary,
                "outcome": outcome,
            }
        )
    return items
