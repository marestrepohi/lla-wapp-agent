"""AWS worker runtime, secret loading, and optional remote vector services."""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    ConnectionClosedError,
    EndpointConnectionError,
    HTTPClientError,
    ReadTimeoutError,
)
from langchain_groq import ChatGroq
from langgraph_checkpoint_aws import DynamoDBSaver

from lla_agent.persistence.dynamodb import DynamoDBRepository
from lla_langchain_agent.agent import DEFAULT_MODEL
from lla_langchain_agent.qdrant_rag import DEFAULT_WEB_COLLECTION, QdrantWebCorpus
from lla_langchain_agent.rag import WebCorpus
from lla_langchain_agent.runtime import AgentRuntime, open_runtime
from .message_store import DynamoDBMessageStore

_LOGGER = logging.getLogger(__name__)
_AWS_CONFIG = Config(
    connect_timeout=2,
    read_timeout=4,
    retries={"mode": "standard", "total_max_attempts": 3},
)
_SECRETS = boto3.client("secretsmanager", config=_AWS_CONFIG)


class SecretUnavailableError(RuntimeError):
    """A transient Secrets Manager failure that can be retried safely."""


class SecretConfigurationError(RuntimeError):
    """A missing, invalid, or unauthorized required secret configuration."""


@lru_cache(maxsize=4)
def _secret(secret_arn: str) -> dict[str, Any]:
    """Read and cache a JSON secret for this Lambda execution environment."""
    response = _SECRETS.get_secret_value(SecretId=secret_arn)
    raw = response.get("SecretString")
    if not isinstance(raw, str):
        raise ValueError("Expected a JSON SecretString")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    return payload


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required Lambda configuration: {name}")
    return value


def _required_secret(secret_arn: str) -> dict[str, Any]:
    try:
        return _secret(secret_arn)
    except ClientError as error:
        details = error.response.get("Error", {})
        code = str(details.get("Code", "")) if isinstance(details, dict) else ""
        metadata = error.response.get("ResponseMetadata", {})
        status_code = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
        if status_code == 429 or (isinstance(status_code, int) and status_code >= 500) or code in {
            "InternalServiceError",
            "ThrottlingException",
            "ServiceUnavailableException",
            "RequestTimeout",
            "PriorRequestNotComplete",
        }:
            raise SecretUnavailableError("Required secret service is temporarily unavailable") from error
        raise SecretConfigurationError("Required secret is unavailable or not authorized") from error
    except (
        ConnectTimeoutError,
        ConnectionClosedError,
        EndpointConnectionError,
        HTTPClientError,
        ReadTimeoutError,
    ) as error:
        raise SecretUnavailableError("Required secret service is temporarily unavailable") from error
    except (BotoCoreError, ValueError, json.JSONDecodeError) as error:
        raise SecretConfigurationError("Required secret has invalid configuration") from error


def configure_whatsapp_token() -> str:
    """Load only the WhatsApp token, allowing REPLY_READY sends without Groq startup."""
    secret_arn = os.getenv("WHATSAPP_SECRET_ARN", "").strip()
    if not secret_arn:
        raise SecretConfigurationError("WHATSAPP_SECRET_ARN is required")
    whatsapp_secret = _required_secret(secret_arn)
    access_token_secret_arn = os.getenv("WHATSAPP_ACCESS_TOKEN_SECRET_ARN", "").strip()
    access_token_secret = (
        _required_secret(access_token_secret_arn) if access_token_secret_arn else whatsapp_secret
    )
    access_token = str(access_token_secret.get("access_token", "")).strip()
    if not access_token:
        raise SecretConfigurationError("WhatsApp access token is missing from its secret")

    os.environ["WHATSAPP_TOKEN"] = access_token
    os.environ["WHATSAPP_GRAPH_API_VERSION"] = os.getenv(
        "WHATSAPP_GRAPH_API_VERSION", "v23.0"
    )
    return access_token


def _configure_required_secrets() -> None:
    configure_whatsapp_token()
    groq_secret = _required_secret(_required_env("GROQ_API_KEY_SECRET_ARN"))
    groq_api_key = str(groq_secret.get("api_key", "")).strip()
    if not groq_api_key:
        raise SecretConfigurationError("Groq API key is missing from its secret")
    os.environ["GROQ_API_KEY"] = groq_api_key


def _configure_optional_qdrant() -> None:
    secret_arn = os.getenv("QDRANT_SECRET_ARN", "").strip()
    if not secret_arn:
        return
    try:
        payload = _secret(secret_arn)
    except (BotoCoreError, ClientError, ValueError, json.JSONDecodeError) as error:
        _LOGGER.warning("Optional Qdrant secret unavailable (%s)", type(error).__name__)
        return

    api_key = str(payload.get("api_key", "")).strip()
    if not api_key:
        _LOGGER.warning("Optional Qdrant secret has no API key")
        return
    os.environ["QDRANT_API_KEY"] = api_key
    secret_url = str(payload.get("url", "")).strip()
    if secret_url:
        os.environ["QDRANT_URL"] = secret_url


async def open_aws_runtime() -> tuple[Any, AgentRuntime, DynamoDBMessageStore]:
    """Build an agent runtime and retain its context manager for the Lambda lifetime."""
    _configure_required_secrets()
    _configure_optional_qdrant()

    customer_table = _required_env("DYNAMODB_CUSTOMER_TABLE")
    conversation_table = _required_env("DYNAMODB_CONVERSATION_TABLE")
    checkpoint_table = _required_env("DYNAMODB_CHECKPOINT_TABLE")
    _required_env("WHATSAPP_PHONE_NUMBER_ID")
    model_name = os.getenv("GROQ_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    dynamodb = boto3.resource("dynamodb", config=_AWS_CONFIG)
    repository = DynamoDBRepository(
        customer_table,
        conversation_table_name=conversation_table,
        dynamodb=dynamodb,
    )
    message_store = DynamoDBMessageStore(
        conversation_table,
        dynamodb=dynamodb,
        config=_AWS_CONFIG,
    )
    checkpointer = DynamoDBSaver(
        table_name=checkpoint_table,
        region_name=os.getenv("AWS_REGION"),
        ttl_seconds=int(os.getenv("CHECKPOINT_TTL_SECONDS", "2592000")),
        enable_checkpoint_compression=True,
        boto_config=_AWS_CONFIG,
    )

    qdrant_url = os.getenv("QDRANT_URL", "").strip()
    qdrant_api_key = os.getenv("QDRANT_API_KEY", "").strip()
    if qdrant_url and qdrant_api_key:
        try:
            web_corpus: Any = QdrantWebCorpus(
                url=qdrant_url,
                api_key=qdrant_api_key,
                collection_name=os.getenv(
                    "QDRANT_WEB_COLLECTION", DEFAULT_WEB_COLLECTION
                ).strip()
                or DEFAULT_WEB_COLLECTION,
                embedding_model=os.getenv(
                    "EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
                ),
            )
        except Exception as error:
            _LOGGER.warning("Optional Qdrant public corpus unavailable (%s)", type(error).__name__)
            web_corpus = WebCorpus()
    else:
        web_corpus = WebCorpus()

    context = open_runtime(
        repository=repository,
        checkpointer=checkpointer,
        model_name=model_name,
        model=ChatGroq(model=model_name, temperature=0, max_retries=1),
        web_corpus=web_corpus,
        qa_id_linking=os.getenv("WHATSAPP_QA_ID_LINKING", "false").casefold()
        in {"1", "true", "yes", "on"},
    )
    runtime = await context.__aenter__()
    _LOGGER.info("AWS WhatsApp agent runtime initialized")
    return context, runtime, message_store
