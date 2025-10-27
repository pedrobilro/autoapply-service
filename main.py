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

from typing import Dict, List, Optional
from pydantic import BaseModel, Field
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

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
    "full_name": "input[name='name'], input[aria-label*='name' i], input[placeholder*='name' i], input#name",
    "email": "input[type='email'], input[name='email'], input[aria-label*='email' i]",
    "phone": "input[type='tel'], input[name='phone'], input[aria-label*='phone' i], input[placeholder*='phone' i]",
    "location": "input[name*='location' i], input[aria-label*='location' i], input[placeholder*='location' i]",
    "current_company": "input[name*='company' i], input[name*='employer' i], input[aria-label*='company' i], input[aria-label*='employer' i], input[placeholder*='company' i], input[placeholder*='employer' i], input[placeholder*='current company' i]",
    "current_location": "input[name*='current' i][name*='location' i], input[aria-label*='current location' i], input[placeholder*='current location' i]",
    "salary": "input[name*='salary' i], input[name*='compensation' i], input[aria-label*='salary' i], input[aria-label*='compensation' i], input[aria-label*='expectations' i], input[placeholder*='salary' i], input[placeholder*='compensation' i]",
    "notice": "input[name*='notice' i], input[name*='availability' i], input[aria-label*='notice' i], input[aria-label*='availability' i], input[aria-label*='notice period' i], input[placeholder*='notice' i], input[placeholder*='availability' i]",
    "additional": "textarea[name*='additional' i], textarea[name*='cover' i], textarea[name*='message' i], textarea[placeholder*='additional' i], textarea[placeholder*='cover' i], textarea[placeholder*='message' i]",
    "resume_file": "input[type='file'][name*='resume' i], input[type='file'][name*='cv' i], input[type='file'][aria-label*='resume' i], input[type='file'][accept*='pdf']",
    "submit": "button:has-text('Submit'), button:has-text('Apply'), button:has-text('Enviar'), button[type='submit']",
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
            if any(k in ln.lower() for k in ["portugal", "lisboa", "lisbon", "porto", "almada", "setúbal"]):
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
        if await loc.is_visible(timeout=2500):
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
        if await loc.is_visible(timeout=2500):
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
        if await btn.is_visible(timeout=2500):
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
                try:
                    submit_btn = page.locator(SELECTORS["submit"]).first
                    if allow_submit and await submit_btn.is_enabled():
                        await submit_btn.click(timeout=5000)
                        log_message(messages, "✓ Clique em Submit")
                    elif not allow_submit:
                        status = "awaiting_consent"
                        log_message(messages, "⚠ allow_submit=False — não submetido")
                    else:
                        log_message(messages, "✗ Não consegui clicar Submit")
                except Exception as e:
                    log_message(messages, f"✗ Erro ao clicar Submit: {e}")

                await asyncio.sleep(2.0)
                if status not in ("awaiting_consent",):
                    ok = await detect_success(page, job_url, messages)
                    status = "submitted" if ok else "not_confirmed"

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
    result = await apply_to_job_async(req.dict())
    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail="critical_error")
    return result
