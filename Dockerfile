FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

# stdio by default; for HTTP: --transport streamable-http --host 0.0.0.0
EXPOSE 8000

CMD ["baidu-mcp-server"]
