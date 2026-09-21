FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
# Пин mcp<2: в requirements.txt объявлено `mcp>=1.0.0` без верхней границы —
# свежая сборка тянет mcp 2.x, где FastMCP переименован в MCPServer, и код
# падает на импорте (см. deploy/toolkit.Dockerfile — та же проблема).
RUN python -m pip install --no-cache-dir -r requirements.txt "mcp>=1.0,<2"

COPY onec_mcp_toolkit_proxy/ ./onec_mcp_toolkit_proxy/

ENV PORT=6003
ENV TIMEOUT=180
ENV LOG_LEVEL=INFO

EXPOSE 6003

CMD ["python", "-m", "onec_mcp_toolkit_proxy"]
