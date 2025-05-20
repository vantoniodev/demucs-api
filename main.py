import os
import uuid
import tempfile
import shutil
import sys
import time
from pathlib import Path
from typing import List, Optional, Dict
import subprocess

import torch
from fastapi import FastAPI, UploadFile, File, BackgroundTasks, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import boto3
from botocore.exceptions import ClientError

# Configuração da aplicação
app = FastAPI(title="Demucs API - Separação de Stems")

# Configurar CORS para permitir acesso do seu front-end
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Substitua pelo domínio específico em produção
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Armazenar jobs em execução
JOBS = {}
AVAILABLE_MODELS = ["htdemucs", "htdemucs_ft", "mdx_extra", "mdx_q"]

# Configurações S3
S3_BUCKET = os.environ.get('AWS_STORAGE_BUCKET_NAME')
s3_client = boto3.client(
    's3',
    aws_access_key_id=os.environ.get('AWS_ACCESS_KEY_ID'),
    aws_secret_access_key=os.environ.get('AWS_SECRET_ACCESS_KEY'),
    region_name=os.environ.get('AWS_REGION', 'us-east-1')
)

# Classe para o resultado da separação
class SeparationResult(BaseModel):
    vocals: str
    drums: str
    bass: str
    other: str
    id: str

# Função para baixar os modelos Demucs
def download_models():
    try:
        print("Iniciando download dos modelos Demucs...")
        # Criar diretório de cache para o modelo
        model_path = Path.home() / ".cache" / "torch" / "hub" / "checkpoints"
        os.makedirs(model_path.parent, exist_ok=True)
        
        # Baixar os modelos um a um (sem processar nenhum áudio)
        for model in AVAILABLE_MODELS:
            print(f"Baixando modelo {model}...")
            cmd = ["python", "-c", f"import torch; import demucs.pretrained; model = demucs.pretrained.get_model('{model}')"]
            subprocess.run(cmd, check=True)
            print(f"Modelo {model} baixado com sucesso")
        
        print("Todos os modelos Demucs foram baixados com sucesso")
    except Exception as e:
        print(f"Erro ao baixar modelos: {e}")

# Upload para o S3
def upload_to_s3(file_path, object_name):
    """Upload a file to S3 bucket"""
    try:
        print(f"Fazendo upload do arquivo {file_path} para S3 como {object_name}")
        s3_client.upload_file(file_path, S3_BUCKET, object_name)
        # Construir URL pública
        url = f"https://{S3_BUCKET}.s3.amazonaws.com/{object_name}"
        print(f"Upload concluído, URL: {url}")
        return url
    except ClientError as e:
        print(f"Erro ao fazer upload para S3: {e}")
        return None

# Função para executar a separação em background
def process_audio(file_path: str, job_id: str, model: str = "htdemucs", two_stems: Optional[str] = None, shifts: int = 1):
    try:
        # Atualizar status
        JOBS[job_id]["status"] = "processing"
        JOBS[job_id]["progress"] = 0.1
        print(f"Iniciando processamento do job {job_id} com modelo {model}")
        
        # Criar diretório temporário para saída
        output_dir = Path(tempfile.mkdtemp())
        print(f"Diretório temporário criado: {output_dir}")
        
        # Configurar argumentos para o Demucs
        args = []
        
        if model:
            args.extend(["-n", model])
        
        if two_stems:
            args.extend(["--two-stems", two_stems])
        
        if shifts > 1:
            args.extend(["--shifts", str(shifts)])
        
        # Adicionar formato e qualidade
        args.extend(["--mp3", "--mp3-bitrate", "320"])
        
        # Adicionar arquivo e diretório de saída
        args.extend(["-o", str(output_dir), file_path])
        
        # Executar Demucs usando sys.argv
        print(f"Executando Demucs com argumentos: {args}")
        JOBS[job_id]["progress"] = 0.2
        
        # Usar sys.argv em vez de passar argumentos diretamente
        original_argv = sys.argv
        sys.argv = ['demucs.separate'] + args
        
        import demucs.separate
        demucs.separate.main()
        
        # Restaurar sys.argv
        sys.argv = original_argv
        
        # Processar os arquivos de saída
        JOBS[job_id]["progress"] = 0.8
        model_dir = output_dir / model
        track_name = Path(file_path).stem
        track_dir = model_dir / track_name
        
        print(f"Processamento concluído, buscando stems em {track_dir}")
        
        # Fazer upload de cada stem para o S3
        stems = {}
        for stem in ["vocals", "drums", "bass", "other"]:
            stem_file = track_dir / f"{stem}.mp3"
            if stem_file.exists():
                print(f"Encontrado arquivo {stem}.mp3, fazendo upload...")
                s3_path = f"{job_id}/{stem}.mp3"
                url = upload_to_s3(str(stem_file), s3_path)
                if url:
                    stems[stem] = url
                else:
                    raise Exception(f"Falha no upload do stem {stem}")
            else:
                print(f"Arquivo {stem}.mp3 não encontrado")
                # Se o formato two_stems foi usado, alguns stems não existirão
                if two_stems:
                    if stem == two_stems:
                        print(f"Stem {stem} esperado, mas não encontrado!")
                    else:
                        print(f"Stem {stem} não esperado para o modo two_stems={two_stems}")
                else:
                    print(f"ALERTA: Stem {stem} não encontrado quando deveria existir")
        
        # Verificar se temos os stems necessários
        if two_stems:
            if two_stems not in stems:
                raise Exception(f"Stem {two_stems} não foi gerado")
        else:
            required_stems = {"vocals", "drums", "bass", "other"}
            missing_stems = required_stems - set(stems.keys())
            if missing_stems:
                raise Exception(f"Os seguintes stems não foram gerados: {', '.join(missing_stems)}")
        
        # Se estivermos no modo two_stems, criar stems fictícios para os outros
        if two_stems:
            primary_stem = two_stems
            accomp_path = f"{job_id}/accompaniment.mp3"
            
            # No modo two_stems, o Demucs cria um arquivo "no_{stem}.mp3" para o acompanhamento
            accomp_file = track_dir / f"no_{primary_stem}.mp3"
            if accomp_file.exists():
                url = upload_to_s3(str(accomp_file), accomp_path)
                
                # Preencher todos os outros stems com a mesma URL do acompanhamento
                for stem in ["vocals", "drums", "bass", "other"]:
                    if stem != primary_stem:
                        stems[stem] = url
        
        result = {**stems, "id": job_id}
        
        # Atualizar status para completo
        JOBS[job_id]["status"] = "completed"
        JOBS[job_id]["progress"] = 1.0
        JOBS[job_id]["result"] = result
        
        print(f"Job {job_id} concluído com sucesso")
        
        # Limpar arquivos temporários
        print(f"Limpando arquivos temporários")
        shutil.rmtree(output_dir)
        os.remove(file_path)
        
    except Exception as e:
        print(f"Erro processando job {job_id}: {e}")
        JOBS[job_id]["status"] = "failed"
        JOBS[job_id]["error"] = str(e)
        
        # Tentar limpar arquivos temporários mesmo em caso de erro
        try:
            if 'output_dir' in locals() and output_dir.exists():
                shutil.rmtree(output_dir)
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception as cleanup_error:
            print(f"Erro ao limpar arquivos temporários: {cleanup_error}")

@app.post("/separate")
async def separate_audio(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    model: str = Form("htdemucs"),
    two_stems: Optional[str] = Form(None),
    shifts: int = Form(1)
):
    # Validar modelo
    if model not in AVAILABLE_MODELS:
        raise HTTPException(status_code=400, detail=f"Modelo inválido. Escolha entre: {', '.join(AVAILABLE_MODELS)}")
    
    # Validar two_stems
    if two_stems and two_stems not in ["vocals", "drums", "bass", "other"]:
        raise HTTPException(status_code=400, detail="two_stems deve ser 'vocals', 'drums', 'bass' ou 'other'")
    
    # Criar ID único para o job
    job_id = str(uuid.uuid4())
    
    # Salvar arquivo temporariamente
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=Path(file.filename).suffix)
    try:
        contents = await file.read()
        with temp_file as f:
            f.write(contents)
        
        print(f"Arquivo recebido: {file.filename}, salvo como {temp_file.name}")
        
        # Inicializar job
        JOBS[job_id] = {
            "status": "queued",
            "progress": 0,
            "file_path": temp_file.name
        }
        
        # Iniciar processamento em background
        background_tasks.add_task(
            process_audio,
            temp_file.name,
            job_id,
            model,
            two_stems,
            shifts
        )
        
        return {"id": job_id, "status": "queued"}
    
    except Exception as e:
        print(f"Erro ao processar upload: {e}")
        if os.path.exists(temp_file.name):
            os.unlink(temp_file.name)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/status/{job_id}")
async def check_status(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job não encontrado")
    
    job = JOBS[job_id]
    response = {
        "status": job["status"],
        "progress": job.get("progress", 0)
    }
    
    if job["status"] == "completed" and "result" in job:
        response["result"] = job["result"]
    elif job["status"] == "failed" and "error" in job:
        response["error"] = job["error"]
    
    return response

@app.get("/models")
async def get_models():
    return AVAILABLE_MODELS

@app.get("/")
async def root():
    return {
        "message": "Demucs API para separação de stems de áudio",
        "version": "1.0.0",
        "endpoints": [
            "/models - Lista modelos disponíveis",
            "/separate - Separar áudio (POST)",
            "/status/{job_id} - Verificar status de um job"
        ]
    }

# Baixar modelos ao iniciar
@app.on_event("startup")
async def startup_event():
    download_models()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
