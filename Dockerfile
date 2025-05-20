FROM python:3.10-slim

WORKDIR /app

# Instalar dependências do sistema
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libsndfile1 \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Configuração para NumPy
ENV NPY_DISABLE_ARRAY_API=1

# Instalar dependências Python
RUN pip install --no-cache-dir numpy==1.25.0
RUN pip install --no-cache-dir torch==2.0.1 torchaudio==2.0.2
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar o código da aplicação
COPY . .

# Expor a porta
EXPOSE 8000

# Comando para iniciar a aplicação
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
