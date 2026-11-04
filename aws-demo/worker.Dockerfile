FROM public.ecr.aws/lambda/python:3.12

COPY --from=ghcr.io/astral-sh/uv:0.8.3 /uv /uvx /usr/local/bin/

WORKDIR ${LAMBDA_TASK_ROOT}

COPY pyproject.toml README.md ${LAMBDA_TASK_ROOT}/
COPY src/ ${LAMBDA_TASK_ROOT}/src/
COPY langchain-agent/pyproject.toml langchain-agent/uv.lock ${LAMBDA_TASK_ROOT}/langchain-agent/
COPY langchain-agent/src/ ${LAMBDA_TASK_ROOT}/langchain-agent/src/
COPY aws-demo/src/ ${LAMBDA_TASK_ROOT}/aws-demo/src/

ENV UV_LINK_MODE=copy
RUN uv sync --frozen --no-dev --extra aws \
    --project ${LAMBDA_TASK_ROOT}/langchain-agent \
    --python /var/lang/bin/python3.12

ENV PATH="${LAMBDA_TASK_ROOT}/langchain-agent/.venv/bin:${PATH}"
ENV PYTHONPATH="${LAMBDA_TASK_ROOT}/aws-demo/src:${LAMBDA_TASK_ROOT}/langchain-agent/src:${LAMBDA_TASK_ROOT}/src:${LAMBDA_TASK_ROOT}/langchain-agent/.venv/lib/python3.12/site-packages"
ENV FASTEMBED_CACHE_PATH=/tmp/fastembed

CMD ["aws_demo.worker.handler"]
