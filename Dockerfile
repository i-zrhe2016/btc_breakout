FROM node:24-slim AS codex-cli

ARG CODEX_CLI_VERSION=0.149.1
RUN npm install --global "@openai/codex@${CODEX_CLI_VERSION}"

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    CODEX_HOME=/codex-home

COPY --from=codex-cli /usr/local/bin/node /usr/local/bin/node
COPY --from=codex-cli /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex \
    && mkdir -p /codex-home

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY main.py /app/main.py
COPY 1.html /app/1.html
COPY strategy.html /app/strategy.html
COPY CODEX_PROMPT.md /app/CODEX_PROMPT.md

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
