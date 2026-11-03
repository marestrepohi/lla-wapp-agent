import asyncio
from datetime import date, timedelta

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from lla_langchain_agent.runtime import open_runtime

from lla_agent.domain.models import InboundMessage, Intent
from lla_agent.persistence.sqlite import SQLiteRepository


def run_scenario(turns):
    async def run():
        repository = SQLiteRepository(":memory:")
        offline_model = FakeListChatModel(responses=["Respuesta simulada no usada."])
        responses = []
        async with open_runtime(
            repository=repository,
            checkpoint_path=":memory:",
            model=offline_model,
        ) as runtime:
            for index, (text, client_id, conversation_id) in enumerate(turns):
                responses.append(
                    await runtime.handle(
                        InboundMessage(
                            text=text,
                            channel="test",
                            message_id=f"message-{index}",
                            external_id=f"test:{client_id}",
                            client_id=client_id,
                            conversation_id=conversation_id,
                        )
                    )
                )
        return repository, responses

    return asyncio.run(run())


def test_balance_always_comes_from_billing_tool():
    _, responses = run_scenario([("¿Cuál es mi saldo?", "C001", "balance")])
    response = responses[0]
    assert response.intent == Intent.BALANCE
    assert "PAB 1,250.00" in response.text
    assert response.tool_calls[0].name == "consultar_saldo"


def test_promise_requires_confirmation_and_then_registers():
    async def run_promise():
        repository = SQLiteRepository(":memory:")
        offline_model = FakeListChatModel(responses=["Respuesta simulada no usada."])
        async with open_runtime(
            repository=repository,
            checkpoint_path=":memory:",
            model=offline_model,
        ) as runtime:
            pending = await runtime.handle(
                InboundMessage(
                    "Quiero pagar todo en 14 días",
                    "test",
                    "promise-request",
                    "test:C001",
                    "C001",
                    "promise",
                )
            )
            pending_promise_count = repository.count_rows("promises")
            confirmation_command = pending.text.split("`")[1]
            confirmed = await runtime.handle(
                InboundMessage(
                    confirmation_command,
                    "test",
                    "promise-confirmation",
                    "test:C001",
                    "C001",
                    "promise",
                )
            )
        return repository, pending, confirmed, pending_promise_count

    repository, pending, confirmed, pending_promise_count = asyncio.run(run_promise())
    assert "confirm" in pending.text.lower()
    assert "PAB 1,250.00" in pending.text
    assert pending_promise_count == 0
    assert repository.count_rows("promises") == 1
    assert confirmed.intent == Intent.CONFIRM_PROMISE
    assert "Promesa confirmada" in confirmed.text
    expected = (date.today() + timedelta(days=14)).isoformat()
    assert repository.all_promises()[0]["payment_date"] == expected


def test_dispute_escalates_after_authoritative_balance_read():
    _, responses = run_scenario([("No reconozco este saldo", "C003", "dispute")])
    response = responses[0]
    assert response.escalated
    assert [call.name for call in response.tool_calls] == [
        "consultar_saldo",
        "escalar_a_humano",
    ]


def test_discount_is_not_applied():
    repository, responses = run_scenario([("Aplícame un descuento", "C002", "discount")])
    assert responses[0].escalated
    assert repository.count_rows("promises") == 0


def test_unknown_client_does_not_disclose_balance():
    _, responses = run_scenario([("¿Cuál es mi saldo?", "C404", "unknown")])
    response = responses[0]
    assert not response.identity_validated
    assert "PAB" not in response.text
    assert not response.tool_calls


def test_tool_error_does_not_invent_balance_and_escalates():
    _, responses = run_scenario([("¿Cuál es mi saldo?", "CERR", "billing-error")])
    response = responses[0]
    assert response.escalated
    assert "No inventaré un monto" in response.text
    assert "consultar_saldo" in [call.name for call in response.tool_calls]


def test_opt_out_is_persisted_and_honored():
    _, responses = run_scenario(
        [
            ("STOP", "C002", "opt-out"),
            ("¿Cuál es mi saldo?", "C002", "opt-out"),
        ]
    )
    first, second = responses
    assert first.opted_out and second.opted_out
    assert second.tool_calls == []
