# chaos-bridge Dockerfile
# ========================
# Imagem mínima para o serviço de compatibilidade com a LocalStack Chaos API.
# Usa Python slim para manter o tamanho pequeno (~100 MB).
#
# Motivação: Precisamos de acesso ao Docker socket do host para pausar/desparar
# o container MiniStack. O usuário "nobody" não tem permissão; rodamos como root
# apenas dentro do container isolado do bridge — o MiniStack em si roda com seu
# próprio isolamento.

FROM python:3.12-slim

WORKDIR /app

# Instala dependências de sistema mínimas
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Copia e instala dependências Python com versões fixadas
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o código da aplicação
COPY app.py .
COPY console ./console

# Expõe a porta do chaos-bridge (4567 para não colidir com a 4566 do MiniStack)
EXPOSE 4567

# Health check do container
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:4567/_chaos_bridge/health || exit 1

# Inicia o servidor ASGI
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "4567", "--log-level", "info"]
