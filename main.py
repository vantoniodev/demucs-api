import os
import uuid
import tempfile
import shutil
from pathlib import Path
from typing import List, Optional, Dict

import torch
from fastapi import FastAPI, UploadFile, File, BackgroundTasks, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# Configuração da aplicação
app = FastAPI(title="Demucs API - Separação de Stems")

# Configurar CORS para permitir acesso do seu front-end
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Substitua pelo domínio de produção
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Armazenar jobs em execução
JOBS = {}
AVAILABLE_MODELS = ["htdemucs", "htdemucs_ft", "mdx_extra", "mdx_q"]

# Classe para o resultado da separação
class SeparationResult(BaseModel):
    vocals: str
    drums: str
    bass: str
    other: str
    id: str

# Função para executar a separação em background
def process_audio(file_path: str, job_id: str, model: str = "htdemucs", two_stems: Optional[str] = None, shifts: int = 1):
    try:
        # Atualizar status
        JOBS[job_id]["status"] = "processing"
        JOBS[job_id]["progress"] = 0.1
        
        # Criar diretório temporário para saída
        output_dir = Path(tempfile.mkdtemp())
        
        # Configurar argumentos para o Demucs
        import demucs.separate
        args = ["--mp3", "--mp3-bitrate", "320"]
        
        if model:
            args.extend(["-n", model])
        
        if two_stems:
            args.extend(["--two-stems", two_stems])
        
        if shifts > 1:
            args.extend(["--shifts", str(shifts)])
        
        # Adicionar arquivo e diretório de saída
        args.extend(["-o", str(output_dir), file_path])
        
        # Executar Demucs
        JOBS[job_id]["progress"] = 0.2
        demucs.separate.main(args)
        
        # Processar os arquivos de saída
        JOBS[job_id]["progress"] = 0.8
        model_dir = output_dir / model
        track_name = Path(file_path).stem
        track_dir = model_dir / track_name
        
        # Aqui você precisaria fazer o upload dos arquivos para algum serviço de storage
        # e obter as URLs públicas para cada stem
        
        # Para este exemplo, estamos apenas simulando URLs
        base_url = os.environ.get("STORAGE_BASE_URL", "https://storage.example.com")
        result = {
            "vocals": f"{base_url}/{job_id}/vocals.mp3",
            "drums": f"{base_url}/{job_id}/drums.mp3",
            "bass": f"{base_url}/{job_id}/bass.mp3",
            "other": f"{base_url}/{job_id}/other.mp3",
            "id": job_id
        }
        
        # Atualizar status para completo
        JOBS[job_id]["status"] = "completed"
        JOBS[job_id]["progress"] = 1.0
        JOBS[job_id]["result"] = result
        
        # Limpar arquivos temporários
        shutil.rmtree(output_dir)
        os.remove(file_path)
        
    except Exception as e:
        JOBS[job_id]["status"] = "failed"
        JOBS[job_id]["error"] = str(e)
        print(f"Error processing job {job_id}: {e}")

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
        with temp_file as f:
            shutil.copyfileobj(file.file, f)
        
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
