import os
import io
import re
import time
import base64
import random
import asyncio
import traceback
import pdfplumber
import httpx
import logging

from typing import Dict, List, Optional
from pydantic import BaseModel, Field
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

# --------------------------
# Logger global
# --------------------------
logger = logging.getLogger("auto-apply-playwright")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s"))
    logger.addHandler(handler)

# --------------------------
# FastAPI app & CORS
# --------------------------
app = FastAPI(title="auto-apply-playwright", version="2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

# --------------------------
# Heurísticas de sucesso
# --------------------------
SUCCESS_HINTS = [
    "thank you", "thanks for applying", "application received",
    "we'll be in touch", "obrigado", "candidatura recebida",
    "application submitted", "successfully applied",
    "we will be in touch", "gracias", "candidatura enviada"
]

# --------------------------
# Selectors genéricos
# --------------------------
SELECTORS = {
    "first_name": "input[name='firstName'], input[aria-label*='first' i], input[placeholder*='first' i]",
    "last_name": "input[name='lastName'], input[aria-label*='last' i], input[placeholder*='last' i]",
    "full_name": "input[name='name'], input[name='full_name'], input[name='fullName'], input[aria-label*='full name' i], input[aria-label*='name' i], input[placeholder*='full name' i], input[placeholder*='name' i], input#name",
    "email": "input[type='email'], input[name='email'], input[aria-label*='email' i], input[placeholder*='email' i]",
    "phone": "input[type='tel'], input[name='phone'], input[name='phoneNumber'], input[name='mobile'], input[aria-label*='phone' i], input[aria-label*='mobile' i], input[placeholder*='phone' i], input[placeholder*='mobile' i]",
    "location": "input[name*='location' i], input[name*='city' i], input[aria-label*='location' i], input[aria-label*='city' i], input[placeholder*='location' i], input[placeholder*='city' i], input[placeholder*='where are you' i]",
    "current_company": "input[name*='company' i], input[name*='employer' i], input[name*='organization' i], input[aria-label*='current company' i], input[aria-label*='company' i], input[aria-label*='employer' i], input[placeholder*='current company' i], input[placeholder*='company' i], input[placeholder*='employer' i]",
    "current_location": "input[name*='currentLocation' i], input[name*='current_location' i], input[name*='currentCity' i], input[aria-label*='current location' i], input[aria-label*='current city' i], input[placeholder*='current location' i], input[placeholder*='current city' i]",
    "salary": "input[name*='salary' i], input[name*='compensation' i], input[name*='expectation' i], input[aria-label*='salary' i], input[aria-label*='compensation' i], input[aria-label*='expectations' i], input[placeholder*='salary' i], input[placeholder*='compensation' i], input[placeholder*='gross' i]",
    "notice": "input[name*='notice' i], input[name*='availability' i], input[name*='noticePeriod' i], input[aria-label*='notice' i], input[aria-label*='availability' i], input[aria-label*='notice period' i], input[placeholder*='notice' i], input[placeholder*='availability' i], input[placeholder*='notice period' i]",
    "additional": "textarea[name*='additional' i], textarea[name*='cover' i], textarea[name*='message' i], textarea[name*='note' i], textarea[placeholder*='additional' i], textarea[placeholder*='cover' i], textarea[placeholder*='message' i], textarea[placeholder*='note' i]",
    "resume_file": "input[type='file'][name*='resume' i], input[type='file'][name*='cv' i], input[type='file'][name*='curriculum' i], input[type='file'][aria-label*='resume' i], input[type='file'][aria-label*='cv' i], input[type='file'][accept*='pdf']",
    "submit": "button:has-text('Submit'), button:has-text('Apply'), button:has-text('Enviar'), button:has-text('Send'), button[type='submit']",
    "open_apply": "a:has-text('Apply'), button:has-text('Apply'), a:has-text('Candidatar'), button:has-text('Candidatar')",
    "required_any": "input[required], textarea[required], select[required], [aria-required='true']",
}

# --------------------------
# Modelos
# --------------------------
class ApplyRequest(BaseModel):
    job_url: str
    full_name: Optional[str] = ""
    email: Optional[str] = ""
    phone: Optional[str] = ""
    location: Optional[str] = ""
    current_company: Optional[str] = ""
    current_location: Optional[str] = ""
    salary_expectations: Optional[str] = ""
    notice_period: Optional[str] = ""
    additional_info: Optional[str] = ""
    resume_url: Optional[str] = None
    resume_b64: Optional[str] = None
    plan_only: bool = False
    allow_submit: bool = True
    openai_api_key: Optional[str] = None

# --------------------------
# Helpers
# --------------------------
def log_message(messages: List[str], msg: str):
    timestamp = time.strftime("%H:%M:%S")
    print(f"[{timestamp}] {msg}", flush=True)
    messages.append(f"[{timestamp}] {msg}")

def extract_from_pdf_bytes(pdf_bytes: bytes) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text = "\n".join([p.extract_text() or "" for p in pdf.pages])
        # Guardar texto bruto para usar em prompts da Vision
        out["__text"] = text
        email = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", text)
        phone = re.search(r"(?:\+?\d{2,3}\s?)?(?:\d[\s\-]?){8,14}\d", text)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        name = None
        for ln in lines[:15]:
            if re.match(r"^[A-ZÀ-Ú][A-Za-zÀ-ú'\-]+(?:\s+[A-ZÀ-Ú][A-Za-zÀ-ú'\-]+){1,2}$", ln):
                name = ln
                break
        loc = None
        for ln in lines:
            if any(k in ln.lower() for k in ["portugal", "lisboa", "lisbon", "porto", "almada", "setúbal", "madrid", "barcelona", "spain", "españa"]):
                loc = ln
                break
        if name: out["full_name"] = name
        if email: out["email"] = email.group(0)
        if phone: out["phone"] = re.sub(r"[^\d+]", "", phone.group(0))
        if loc: out["location"] = loc
    except Exception:
        pass
    return out

async def load_resume_bytes(resume_url: Optional[str], resume_b64: Optional[str]) -> Optional[bytes]:
    if resume_b64:
        try:
            return base64.b64decode(resume_b64)
        except Exception:
            return None
    if resume_url:
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                r = await client.get(resume_url)
                r.raise_for_status()
                return r.content
        except Exception:
            return None
    return None

# --------------------------
# Playwright helpers
# --------------------------
async def fill_field(page, selector: str, value: str, messages: List[str]) -> bool:
    if not value:
        return False
    try:
        loc = page.locator(selector).first
        await loc.wait_for(state="visible", timeout=2500)
        if await loc.is_visible():
            await loc.fill(value)
            log_message(messages, f"✓ Preencheu {selector[:45]} -> '{value[:42]}'")
            await asyncio.sleep(random.uniform(0.3, 0.7))
            return True
    except Exception as e:
        log_message(messages, f"✗ Falha fill {selector[:40]}: {e}")
    return False

async def fill_autocomplete(page, selector: str, value: str, messages: List[str]) -> bool:
    if not value:
        return False
    try:
        loc = page.locator(selector).first
        await loc.wait_for(state="visible", timeout=2500)
        if await loc.is_visible():
            await loc.click()
            await loc.fill(value)
            await asyncio.sleep(random.uniform(0.4, 0.8))
            await page.keyboard.press("ArrowDown")
            await page.keyboard.press("Enter")
            log_message(messages, f"✓ Auto-complete: {value}")
            return True
    except Exception as e:
        log_message(messages, f"✗ Falha autocomplete {selector[:40]}: {e}")
    return False

async def upload_resume(page, pdf_bytes: Optional[bytes], messages: List[str]) -> bool:
    if not pdf_bytes:
        return False
    try:
        tmp_path = "/tmp/_resume.pdf"
        with open(tmp_path, "wb") as f:
            f.write(pdf_bytes)
        file_input = page.locator(SELECTORS["resume_file"]).first
        if await file_input.count() == 0:
            file_input = page.locator("input[type='file']").first
        if await file_input.count() > 0:
            await file_input.set_input_files(tmp_path)
            log_message(messages, "✓ Currículo carregado")
            return True
        log_message(messages, "⚠ Nenhum input[type=file] encontrado")
    except Exception as e:
        log_message(messages, f"✗ Erro upload CV: {e}")
    return False

async def try_open_apply_modal(page, messages: List[str]):
    try:
        btn = page.locator(SELECTORS["open_apply"]).first
        await btn.wait_for(state="visible", timeout=2500)
        if await btn.is_visible():
            await btn.click()
            log_message(messages, "✓ Abriu formulário Apply")
            await asyncio.sleep(1.0)
    except Exception:
        pass

async def check_required_errors(page, messages: List[str]) -> List[str]:
    problems = []
    try:
        invalids = page.locator(":invalid")
        n = await invalids.count()
        for i in range(min(n, 10)):
            el = invalids.nth(i)
            name = await el.get_attribute("name")
            problems.append(f"invalid:{name or '?'}")
    except Exception:
        pass
    html = (await page.content()).lower()
    for needle in ["please fill out this field", "campo obrigatório", "required"]:
        if needle in html:
            problems.append(f"text:{needle}")
    if problems:
        log_message(messages, f"⚠ Problemas de validação: {problems}")
    return problems

async def analyze_screenshot_with_vision(screenshot_b64: str, messages: List[str], openai_key: Optional[str] = None, cv_text: Optional[str] = None, user_data: Optional[Dict[str, str]] = None) -> Dict:
    """
    Envia screenshot + contexto do CV para GPT Vision e recebe análise:
    - success: True/False
    - reason: explicação
    - instructions: lista de ações para corrigir (se não foi sucesso)
    """
    if not openai_key:
        log_message(messages, "⚠ OPENAI_API_KEY não fornecida - pulando Vision")
        return {"success": False, "reason": "API key not provided", "instructions": []}
    
    try:
        log_message(messages, "🔍 Analisando screenshot com GPT-5 Vision...")
        # Compactar CV text para não estourar tokens
        cv_excerpt = None
        if cv_text:
            trimmed = cv_text.strip()
            cv_excerpt = trimmed[:4000]  # suficiente
        known_fields = {k: v for k, v in (user_data or {}).items() if k in [
            "full_name","email","phone","location","current_company","current_location","salary_expectations","notice_period"
        ] and v}
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {openai_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "gpt-4o",
                    "temperature": 0.3,
                    "max_tokens": 800,
                    "messages": [
                        {
                            "role": "system",
                            "content": """You are an AI that analyzes job application form screenshots and outputs STRICT JSON only. Never use markdown fences. Use the candidate CV text to infer missing values. Prefer label-based actions using accessible names (as visible on the form). If a CAPTCHA iframe is present, set captcha_type to 'iframe'."""
                        },
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": (
                                    "Analyze this job application screenshot. Decide if submission succeeded. "
                                    "If not, generate precise Playwright-friendly instructions using label-based selectors. "
                                    "ALWAYS provide values derived from the CV when a field is empty (e.g., job title, legal name, city, phone, email). "
                                    "Known fields: " + str(known_fields) + "\n\nCV Text (may be truncated):\n" + (cv_excerpt or "")
                                )},
                                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}}
                            ]
                        }
                    ]
                }
            )
            
            if response.status_code != 200:
                error_text = response.text
                log_message(messages, f"✗ Vision API error: {response.status_code}")
                log_message(messages, f"✗ Response: {error_text[:200]}")
                return {"success": False, "reason": "API error", "instructions": []}
            
            data = response.json()
            log_message(messages, f"📥 API Response status: OK")
            
            if "choices" not in data or not data["choices"]:
                log_message(messages, f"✗ Response inválida: {str(data)[:200]}")
                return {"success": False, "reason": "Invalid API response", "instructions": []}
            
            content = data["choices"][0]["message"]["content"]
            log_message(messages, f"📄 Content recebido: {content[:100]}...")
            
            # Limpar markdown code blocks se existirem
            content_clean = content.strip()
            if content_clean.startswith("```json"):
                content_clean = content_clean[7:]
            if content_clean.startswith("```"):
                content_clean = content_clean[3:]
            if content_clean.endswith("```"):
                content_clean = content_clean[:-3]
            content_clean = content_clean.strip()
            
            # Parse JSON from response
            import json
            import re
            try:
                result = json.loads(content_clean)
            except json.JSONDecodeError as e:
                log_message(messages, f"✗ Erro JSON decode: {e}, tentando extrair com regex...")
                # Fallback: tentar extrair JSON com regex
                json_match = re.search(r'\{[\s\S]*\}', content_clean)
                if json_match:
                    result = json.loads(json_match.group(0))
                else:
                    log_message(messages, f"✗ Não foi possível extrair JSON do conteúdo")
                    return {"success": False, "reason": "Failed to parse Vision response", "instructions": []}
            
            if result.get("success"):
                log_message(messages, f"✓ Vision confirmou sucesso: {result.get('reason', '')}")
            else:
                log_message(messages, f"✗ Vision detectou falha: {result.get('reason', '')}")
                instructions = result.get("instructions", [])
                captcha_type = result.get("captcha_type")
                captcha_prompt = result.get("captcha_prompt")
                
                if captcha_type:
                    log_message(messages, f"🔐 CAPTCHA detectado: {captcha_type}")
                    if captcha_prompt:
                        log_message(messages, f"   Prompt: {captcha_prompt}")
                    if captcha_type != "iframe":
                        log_message(messages, "   Vision vai tentar resolver...")
                
                if instructions:
                    log_message(messages, f"📋 Instruções recebidas: {len(instructions)} ações")
                    for idx, inst in enumerate(instructions, 1):
                        log_message(messages, f"   {idx}. {inst}")
            
            return result
            
    except Exception as e:
        log_message(messages, f"✗ Erro ao analisar com Vision: {e}")
        return {"success": False, "reason": str(e), "instructions": []}


async def execute_vision_instructions(page, instructions: List[str], messages: List[str]) -> bool:
    """
    Executa as instruções fornecidas pelo Vision API
    """
    if not instructions:
        return False
    
    log_message(messages, f"🔧 Executando {len(instructions)} instruções do Vision...")
    executed_count = 0
    
    for i, instruction in enumerate(instructions, 1):
        try:
            # Skip unsolvable iframe CAPTCHAs only
            if "UNSOLVABLE_IFRAME" in instruction:
                log_message(messages, f"  [{i}] ⚠ CAPTCHA iframe não pode ser resolvido - a pular")
                continue
            
            log_message(messages, f"  [{i}] Executando: {instruction}")
            
            # Parse instruction and execute
            instruction_lower = instruction.lower()
            
            # Handle CAPTCHA image clicks by position
            if "click captcha image at position" in instruction_lower:
                match = re.search(r"position\s*\((\d+),\s*(\d+)\)", instruction)
                if match:
                    row, col = int(match.group(1)), int(match.group(2))
                    try:
                        # Find CAPTCHA grid container and click specific image
                        # Most captchas use grid layout, so we calculate nth-child
                        # Assuming 3 columns per row (adjust if needed)
                        cols_per_row = 3
                        image_index = (row - 1) * cols_per_row + col
                        
                        # Try multiple selectors for captcha images
                        selectors = [
                            f".captcha-grid img:nth-child({image_index})",
                            f"[class*='captcha'] img:nth-child({image_index})",
                            f"img[alt*='captcha']:nth-child({image_index})",
                            f".rc-imageselect-tile:nth-child({image_index})",
                        ]
                        
                        clicked = False
                        for selector in selectors:
                            try:
                                el = page.locator(selector).first
                                if await el.count() > 0:
                                    await el.click(timeout=2000)
                                    log_message(messages, f"    ✓ Clicou em imagem CAPTCHA ({row},{col})")
                                    clicked = True
                                    executed_count += 1
                                    break
                            except:
                                continue
                        
                        if not clicked:
                            # Fallback: get all images and click by index
                            all_imgs = page.locator("img")
                            count = await all_imgs.count()
                            if image_index <= count:
                                await all_imgs.nth(image_index - 1).click(timeout=2000)
                                log_message(messages, f"    ✓ Clicou em imagem CAPTCHA ({row},{col}) via fallback")
                                executed_count += 1
                    except Exception as e:
                        log_message(messages, f"    ✗ Falha ao clicar em CAPTCHA image: {e}")
                continue
            
            # Handle CAPTCHA submit button
            if "click captcha submit" in instruction_lower:
                try:
                    selectors = [
                        "button:has-text('Submit')",
                        "button:has-text('Verify')",
                        "[class*='captcha'] button[type='submit']",
                        ".captcha-submit",
                        "#captcha-submit"
                    ]
                    for selector in selectors:
                        try:
                            btn = page.locator(selector).first
                            if await btn.is_visible(timeout=2000):
                                await btn.click()
                                log_message(messages, f"    ✓ Clicou em botão submit do CAPTCHA")
                                executed_count += 1
                                break
                        except:
                            continue
                except Exception as e:
                    log_message(messages, f"    ✗ Falha ao clicar submit CAPTCHA: {e}")
                continue
            
            if "select option" in instruction_lower and "dropdown" in instruction_lower:
                # Extract option value and dropdown identifier
                match = re.search(r"select option ['\"](.+?)['\"] in dropdown\s*(?:\[name=['\"](.+?)['\"]\]|['\"](.+?)['\"])", instruction, re.IGNORECASE)
                if match:
                    option_value = match.group(1)
                    dropdown_name = match.group(2) or match.group(3)
                    
                    try:
                        # Try by name attribute first
                        if match.group(2):
                            select_el = page.locator(f"select[name='{dropdown_name}']")
                        else:
                            # Try by label text
                            label = page.locator(f"label:has-text('{dropdown_name}')")
                            select_id = await label.get_attribute("for") if await label.count() > 0 else None
                            if select_id:
                                select_el = page.locator(f"select#{select_id}")
                            else:
                                # Fallback: any select near the label
                                select_el = page.locator("select").first
                        
                        await select_el.select_option(label=option_value, timeout=3000)
                        log_message(messages, f"    ✓ Dropdown selecionado: {option_value}")
                        executed_count += 1
                    except Exception as e:
                        log_message(messages, f"    ✗ Falha ao selecionar dropdown: {e}")
                        
            elif "fill" in instruction_lower:
                # Extract selector and value
                match = re.search(r"fill\s+(.+?)\s+with\s+(?:value\s+)?['\"](.+?)['\"]", instruction, re.IGNORECASE)
                if match:
                    selector, value = match.groups()
                    if await fill_field(page, selector.strip(), value.strip(), messages):
                        executed_count += 1
                    
            elif "click" in instruction_lower:
                # Extract what to click
                match = re.search(r"click\s+(.+)", instruction, re.IGNORECASE)
                if match:
                    target = match.group(1).strip()
                    try:
                        # Try to click by text first
                        if "text" in target.lower() or "'" in target or '"' in target:
                            text = re.search(r"['\"](.+?)['\"]", target)
                            if text:
                                btn = page.locator(f"button:has-text('{text.group(1)}'), a:has-text('{text.group(1)}')")
                                await btn.first.click(timeout=3000)
                        else:
                            # Try as selector
                            await page.locator(target).first.click(timeout=3000)
                        log_message(messages, f"    ✓ Clicou em: {target[:40]}")
                        executed_count += 1
                    except Exception as e:
                        log_message(messages, f"    ✗ Falha ao clicar: {e}")
                        
            elif "select" in instruction_lower:
                match = re.search(r"select\s+(?:option\s+)?['\"](.+?)['\"]\s+in\s+(.+)", instruction, re.IGNORECASE)
                if match:
                    value, selector = match.groups()
                    try:
                        await page.locator(selector.strip()).select_option(value.strip())
                        log_message(messages, f"    ✓ Selecionou: {value}")
                        executed_count += 1
                    except Exception as e:
                        log_message(messages, f"    ✗ Falha ao selecionar: {e}")
                        
            elif "check" in instruction_lower:
                match = re.search(r"check\s+(.+)", instruction, re.IGNORECASE)
                if match:
                    selector = match.group(1).strip()
                    try:
                        await page.locator(selector).check(timeout=3000)
                        log_message(messages, f"    ✓ Marcou checkbox: {selector[:40]}")
                        executed_count += 1
                    except Exception as e:
                        log_message(messages, f"    ✗ Falha ao marcar: {e}")
            
            await asyncio.sleep(random.uniform(0.5, 1.0))
            
        except Exception as e:
            log_message(messages, f"    ✗ Erro ao executar instrução: {e}")
    
    log_message(messages, f"✓ Executadas {executed_count}/{len(instructions)} instruções com sucesso")
    return executed_count > 0


async def detect_success(page, job_url: str, messages: List[str]) -> bool:
    ok = False
    try:
        await page.wait_for_timeout(1500)
        html = (await page.content()).lower()
        if any(h in html for h in SUCCESS_HINTS):
            log_message(messages, "✓ Texto de sucesso detectado")
            ok = True
        try:
            await page.wait_for_url(lambda u: u != job_url, timeout=4000)
            log_message(messages, "✓ URL alterou após submissão")
            ok = True
        except PwTimeout:
            pass
        try:
            submit_btn = page.locator(SELECTORS["submit"]).first
            if await submit_btn.count() == 0:
                log_message(messages, "✓ Botão Submit desapareceu")
                ok = True
            elif await submit_btn.is_disabled():
                log_message(messages, "✓ Botão Submit desactivado")
                ok = True
        except Exception:
            pass
    except Exception as e:
        log_message(messages, f"⚠ Erro ao detectar sucesso: {e}")
    return ok

# --------------------------
# Core
# --------------------------
async def apply_to_job_async(user_data: Dict[str, str]) -> Dict:
    messages: List[str] = []
    t0 = time.time()
    job_url = user_data.get("job_url", "")
    plan_only = bool(user_data.get("plan_only", False))
    allow_submit = bool(user_data.get("allow_submit", True))
    openai_api_key = user_data.get("openai_api_key")

    pdf_bytes = await load_resume_bytes(user_data.get("resume_url"), user_data.get("resume_b64"))
    if pdf_bytes:
        extracted = extract_from_pdf_bytes(pdf_bytes)
        for k, v in extracted.items():
            user_data.setdefault(k, v)
        log_message(messages, f"✓ CV parse: {list(extracted.keys()) or 'nenhum'}")

    required = ["job_url", "email"]
    missing = [f for f in required if not user_data.get(f)]
    if missing:
        return {"ok": False, "status": "missing_fields", "missing": missing, "log": messages}

    screenshot_b64 = None
    ok = False
    status = "unknown"

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = await browser.new_page()
            page.set_default_timeout(15000)

            log_message(messages, f"Iniciando candidatura: {job_url}")
            await page.goto(job_url, wait_until="domcontentloaded")
            await asyncio.sleep(1.2)
            await try_open_apply_modal(page, messages)
            if pdf_bytes:
                await upload_resume(page, pdf_bytes, messages)

            filled_name = await fill_field(page, SELECTORS["full_name"], user_data.get("full_name", ""), messages)
            if not filled_name and user_data.get("full_name"):
                parts = user_data["full_name"].split(maxsplit=1)
                first = parts[0]
                last = parts[1] if len(parts) > 1 else ""
                await fill_field(page, SELECTORS["first_name"], first, messages)
                await fill_field(page, SELECTORS["last_name"], last, messages)

            await fill_field(page, SELECTORS["email"], user_data.get("email", ""), messages)
            await fill_field(page, SELECTORS["phone"], user_data.get("phone", ""), messages)

            loc_val = user_data.get("location") or user_data.get("current_location", "")
            if not await fill_autocomplete(page, SELECTORS["location"], loc_val, messages):
                await fill_field(page, SELECTORS["location"], loc_val, messages)

            await fill_field(page, SELECTORS["current_company"], user_data.get("current_company", ""), messages)
            cloc_val = user_data.get("current_location", "")
            if not await fill_autocomplete(page, SELECTORS["current_location"], cloc_val, messages):
                await fill_field(page, SELECTORS["current_location"], cloc_val, messages)

            await fill_field(page, SELECTORS["salary"], user_data.get("salary_expectations", ""), messages)
            await fill_field(page, SELECTORS["notice"], user_data.get("notice_period", ""), messages)
            await fill_field(page, SELECTORS["additional"], user_data.get("additional_info", ""), messages)

            problems = await check_required_errors(page, messages)
            if problems:
                await asyncio.sleep(0.8)
                problems = await check_required_errors(page, messages)

            if plan_only:
                status = "planned_only"
            else:
                # Self-healing loop com Vision AI (max 3 tentativas)
                MAX_RETRIES = 3
                retry_count = 0
                
                while retry_count < MAX_RETRIES:
                    retry_count += 1
                    log_message(messages, f"🔄 Tentativa {retry_count}/{MAX_RETRIES}")
                    
                    try:
                        submit_btn = page.locator(SELECTORS["submit"]).first
                        if allow_submit and await submit_btn.is_enabled():
                            await submit_btn.click(timeout=5000)
                            log_message(messages, "✓ Clique em Submit")
                        elif not allow_submit:
                            status = "awaiting_consent"
                            log_message(messages, "⚠ allow_submit=False — não submetido")
                            break
                        else:
                            log_message(messages, "✗ Não consegui clicar Submit")
                    except Exception as e:
                        log_message(messages, f"✗ Erro ao clicar Submit: {e}")

                    await asyncio.sleep(2.0)
                    
                    # Tirar screenshot para análise
                    try:
                        png = await page.screenshot(full_page=True)
                        screenshot_b64 = base64.b64encode(png).decode("utf-8")
                        log_message(messages, "✓ Screenshot capturado")
                    except Exception as e:
                        log_message(messages, f"✗ Erro ao capturar screenshot: {e}")
                        break
                    
                    # Detectar sucesso com heurísticas básicas
                    basic_success = await detect_success(page, job_url, messages)
                    
                    # Analisar com Vision AI
                    vision_result = await analyze_screenshot_with_vision(screenshot_b64, messages, openai_api_key)
                    
                    # Se Vision confirma sucesso OU heurística detectou
                    if vision_result.get("success") or basic_success:
                        ok = True
                        status = "submitted"
                        log_message(messages, "🎉 Candidatura confirmada com sucesso!")
                        break
                    
                    # Se não foi sucesso e temos instruções do Vision
                    instructions = vision_result.get("instructions", [])
                    if instructions and retry_count < MAX_RETRIES:
                        log_message(messages, f"🔧 Vision detectou problemas. A corrigir...")
                        await execute_vision_instructions(page, instructions, messages)
                        await asyncio.sleep(1.0)
                        # Loop continua para nova tentativa
                    else:
                        # Sem instruções ou última tentativa
                        ok = False
                        status = "not_confirmed"
                        log_message(messages, "✗ Não foi possível confirmar sucesso")
                        break
                
                if retry_count >= MAX_RETRIES and not ok:
                    log_message(messages, f"⚠ Atingiu {MAX_RETRIES} tentativas sem sucesso confirmado")
                    status = "max_retries_reached"

            # Screenshot final (se ainda não tirado)
            if not screenshot_b64:
                try:
                    png = await page.screenshot(full_page=True)
                    screenshot_b64 = base64.b64encode(png).decode("utf-8")
                    log_message(messages, "✓ Screenshot final capturado")
                except Exception:
                    pass

            await browser.close()

    except Exception as e:
        tb = traceback.format_exc()
        log_message(messages, f"✗ ERRO CRÍTICO: {e}\n{tb}")
        status = "error"
        ok = False

    elapsed = round(time.time() - t0, 2)
    return {
        "ok": ok,
        "status": status,
        "job_url": job_url,
        "elapsed_s": elapsed,
        "log": messages,
        "screenshot": screenshot_b64,
    }

# --------------------------
# Endpoints
# --------------------------
@app.get("/")
def root():
    return {"status": "healthy", "service": "auto-apply-playwright", "version": "2.0"}

@app.get("/health")
def health():
    return {"status": "healthy", "service": "auto-apply-playwright", "version": "2.0"}

@app.post("/apply")
async def auto_apply(req: ApplyRequest):
    try:
        logger.info(f"📥 Recebendo request para job: {req.job_url}")
        logger.info(f"📋 Dados: name={req.full_name}, email={req.email}, phone={req.phone}")
        
        result = await apply_to_job_async(req.dict())
        
        logger.info(f"✅ Resultado: status={result.get('status')}, ok={result.get('ok')}")
        
        if result.get("status") == "error":
            error_msg = result.get("error", "Erro desconhecido")
            logger.error(f"❌ Aplicação falhou: {error_msg}")
            logger.error(f"❌ Log completo: {result.get('log', [])}")
            raise HTTPException(
                status_code=500, 
                detail={
                    "error": error_msg,
                    "log": result.get("log", [])
                }
            )
        
        return result
    except HTTPException:
        raise
    except Exception as e:
        try:
            logger.error(f"❌ ERRO CRÍTICO: {type(e).__name__}: {str(e)}", exc_info=True)
        except Exception:
            print(f"❌ ERRO CRÍTICO: {type(e).__name__}: {str(e)}", flush=True)
        raise HTTPException(
            status_code=500,
            detail={
                "error": str(e),
                "type": type(e).__name__
            }
        )
