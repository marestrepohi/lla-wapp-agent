from datetime import date, timedelta

import pytest

from lla_agent.persistence.sqlite import SQLiteRepository, ToolError
from lla_agent.tools.business import BillingTool, PaymentsTool


def test_billing_tool_reads_seeded_balance():
    tool = BillingTool(SQLiteRepository(":memory:"))
    balance = tool.consultar_saldo("C001")
    assert balance.amount_cents == 125000
    assert balance.currency == "PAB"


def test_billing_tool_surfaces_source_failure():
    tool = BillingTool(SQLiteRepository(":memory:"))
    with pytest.raises(ToolError):
        tool.consultar_saldo("CERR")


def test_payment_tool_validates_amount_and_date():
    tool = PaymentsTool(SQLiteRepository(":memory:"))
    with pytest.raises(ValueError):
        tool.registrar_promesa_pago("C001", 0, date.today() + timedelta(days=1))
    with pytest.raises(ValueError):
        tool.registrar_promesa_pago("C001", 100, date.today() - timedelta(days=1))
