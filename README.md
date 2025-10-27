# Auto-Apply Playwright Service v2.0

Microserviço Python com Playwright para automatizar candidaturas. Inclui parsing de CVs, validação inteligente e screenshots.

## 🚀 Deploy no Railway

### 1. Preparar GitHub
1. Cria um repositório no GitHub (ex: `autoapply-service`)
2. Faz upload dos ficheiros desta pasta:
   - `main.py`
   - `requirements.txt`
   - `Dockerfile`
   - `railway.json`
   - `README.md`

### 2. Deploy no Railway
1. Vai a [railway.app](https://railway.app) e faz login
2. "New Project" → "Deploy from GitHub repo"
3. Seleciona o repositório
4. Railway vai detetar o Dockerfile e fazer deploy (5-10 min)

### 3. Obter URL do Serviço
1. No dashboard Railway → "Settings" → "Networking"
2. Clica "Generate Domain"
3. Copia o URL (ex: `https://auto-apply-production.up.railway.app`)

### 4. Configurar no Lovable Cloud
1. Adiciona o secret `PYTHON_SERVICE_URL` com o URL do Railway
2. A edge function `auto-apply-external` já está configurada

## 🧪 Testar Localmente

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
uvicorn main:app --port 8080 --reload

# Testar
curl -X POST http://localhost:8080/apply \
  -H "Content-Type: application/json" \
  -d '{
    "job_url": "https://jobs.lever.co/example/job",
    "email": "pedro@example.com",
    "full_name": "Pedro Bilro",
    "phone": "+351912345678",
    "resume_url": "https://example.com/cv.pdf",
    "plan_only": false,
    "allow_submit": true
  }'
```

## Endpoints

- `GET /` - Health check
- `GET /health` - Status do serviço
- `POST /apply` - Executar candidatura automática

### POST /apply

**Request:**
```json
{
  "job_url": "https://jobs.lever.co/company/job-id",
  "full_name": "Nome Completo",
  "email": "email@example.com",
  "phone": "+351 900 000 000"
}
```

**Response:**
```json
{
  "ok": true,
  "job_url": "https://jobs.lever.co/company/job-id",
  "elapsed_s": 12.5,
  "screenshot": "base64_encoded_screenshot...",
  "log": [
    "[10:30:45] Iniciando candidatura...",
    "[10:30:47] ✓ Preencheu email...",
    "..."
  ]
}
```

## Custos Railway

- **Free tier**: $5 de crédito/mês
- **Custo por execução**: ~$0.01-0.02
- **Estimativa**: 200-500 candidaturas/mês no free tier
