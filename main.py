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
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

try:
    from twocaptcha import TwoCaptcha
    TWOCAPTCHA_AVAILABLE = True
except ImportError:
    TWOCAPTCHA_AVAILABLE = False
    logger.warning("2captcha-python não disponível - resolução de CAPTCHA desabilitada")

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
    "submit_strict": "button:has-text('Submit application'), button:has-text('Submit Application'), button[data-qa='apply-form-submit']",
    "open_apply": "a:has-text('Apply'), button:has-text('Apply'), a:has-text('Candidatar'), button:has-text('Candidatar')",
    "gh_form": "[data-qa='application-form'], form[action*='applications'], form",
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
        await loc.wait_for(state="visible", timeout=8000)
        if await loc.is_visible():
            await loc.scroll_into_view_if_needed()
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

async def fill_by_possible_labels(page, labels: List[str], value: str, messages: List[str]) -> bool:
    """Tenta preencher campo por vários labels possíveis"""
    if not value:
        return False
    for lb in labels:
        try:
            el = page.get_by_label(lb)
            await el.fill(value, timeout=8000)
            log_message(messages, f"✓ Preenchido por label '{lb}' -> '{value[:40]}'")
            return True
        except Exception:
            continue
    return False

async def fill_autocomplete_location(page, location: str, messages: List[str]) -> bool:
    """Preenche campo de localização com autocomplete (tipo Greenhouse)"""
    if not location:
        return False
    
    try:
        # Tenta encontrar o campo de localização
        city_selectors = [
            "input[aria-label*='Location' i]",
            "input[aria-label*='City' i]",
            "input[name*='location' i]",
            "input[placeholder*='location' i]",
            "[role='combobox'][aria-label*='Location' i]"
        ]
        
        for selector in city_selectors:
            try:
                city_field = page.locator(selector).first
                if await city_field.is_visible(timeout=3000):
                    # Scroll e clica no campo
                    await city_field.scroll_into_view_if_needed()
                    await city_field.click(timeout=5000)
                    await asyncio.sleep(0.3)
                    
                    # Preenche o valor
                    await city_field.fill(location, timeout=8000)
                    await asyncio.sleep(0.5)
                    
                    # Tenta selecionar da dropdown (ArrowDown + Enter)
                    try:
                        await page.wait_for_selector('[role="listbox"], [role="list"], .dropdown-menu', timeout=3000)
                        await asyncio.sleep(0.3)
                        await page.keyboard.press("ArrowDown")
                        await asyncio.sleep(0.2)
                        await page.keyboard.press("Enter")
                        log_message(messages, f"✓ Location selecionada via dropdown: {location}")
                        return True
                    except Exception:
                        # Se não houver dropdown, apenas Enter
                        await page.keyboard.press("Enter")
                        log_message(messages, f"✓ Location preenchida (sem dropdown): {location}")
                        return True
            except Exception:
                continue
        
        log_message(messages, "⚠ Campo Location não encontrado com seletores específicos")
        return False
    except Exception as e:
        log_message(messages, f"✗ Erro ao preencher Location: {e}")
        return False

async def expand_collapsed_sections(page, messages: List[str]):
    """Expande secções colapsadas (Additional Information, etc.)"""
    toggle_texts = ["Additional", "More", "Details", "Optional", "Informação Adicional"]
    expanded = 0
    
    for txt in toggle_texts:
        try:
            toggles = page.get_by_role("button", name=re.compile(txt, re.I))
            count = await toggles.count()
            for i in range(count):
                toggle = toggles.nth(i)
                if await toggle.is_visible(timeout=1000):
                    await toggle.scroll_into_view_if_needed()
                    await toggle.click(timeout=2000)
                    expanded += 1
                    await asyncio.sleep(0.3)
        except Exception:
            continue
    
    if expanded:
        log_message(messages, f"✓ Expandidas {expanded} secções colapsadas")
    return expanded

async def autofix_required_fields(page, messages: List[str]) -> int:
    fixed = 0
    # Dropdowns obrigatórios (HTML select)
    try:
        selects = page.locator("select[required], select[aria-required='true']")
        count = await selects.count()
        for i in range(count):
            sel = selects.nth(i)
            try:
                current = await sel.input_value()
            except Exception:
                current = ""
            if not current:
                options = await sel.locator("option").all()
                for opt in options:
                    label = (await opt.text_content()) or ""
                    value = (await opt.get_attribute("value")) or ""
                    if value and value.strip() and "select" not in label.lower():
                        try:
                            await sel.select_option(value=value)
                            fixed += 1
                            break
                        except Exception:
                            continue
    except Exception:
        pass

    # Combobox customizados (tipo React-Select) - usar click + keyboard
    try:
        comboboxes = page.locator("[role='combobox'][aria-required='true'], [role='combobox'][required]")
        cbo_count = await comboboxes.count()
        for i in range(cbo_count):
            cbo = comboboxes.nth(i)
            try:
                await cbo.scroll_into_view_if_needed()
                await cbo.click(timeout=3000)
                await asyncio.sleep(0.3)
                # Escolhe primeira opção visível
                await page.keyboard.press("ArrowDown")
                await asyncio.sleep(0.2)
                await page.keyboard.press("Enter")
                fixed += 1
                log_message(messages, f"✓ Combobox customizado preenchido")
            except Exception:
                continue
    except Exception:
        pass

    # Checkboxes obrigatórios (GDPR / privacy / terms)
    try:
        checkboxes = page.locator("input[type='checkbox'][required], input[type='checkbox'][aria-required='true']")
        ccount = await checkboxes.count()
        for i in range(ccount):
            cb = checkboxes.nth(i)
            try:
                if not await cb.is_checked():
                    await cb.check()
                    fixed += 1
            except Exception:
                continue
    except Exception:
        pass

    # Radios obrigatórios
    try:
        radios = page.locator("input[type='radio'][required], input[type='radio'][aria-required='true']")
        seen = set()
        rcount = await radios.count()
        for i in range(rcount):
            rd = radios.nth(i)
            name = (await rd.get_attribute("name")) or f"__rd{i}"
            if name in seen:
                continue
            seen.add(name)
            try:
                await rd.check()
                fixed += 1
            except Exception:
                pass
    except Exception:
        pass

    # Inputs/textarea required vazios
    try:
        req_inputs = page.locator("input[required], textarea[required], [aria-required='true']")
        icount = await req_inputs.count()
        for i in range(icount):
            inp = req_inputs.nth(i)
            try:
                tag = (await inp.evaluate("el => el.tagName")) or "INPUT"
            except Exception:
                tag = "INPUT"
            try:
                t = (await inp.get_attribute("type")) or ""
            except Exception:
                t = ""
            if tag == "TEXTAREA" or t in ("text","search","tel","url","number",""):
                val = ""
                try:
                    val = await inp.input_value()
                except Exception:
                    pass
                if not val:
                    try:
                        await inp.fill("N/A")
                        fixed += 1
                    except Exception:
                        pass
    except Exception:
        pass

    if fixed:
        log_message(messages, f"✓ autofix_required_fields: corrigiu {fixed} campos obrigatórios")
    return fixed

async def try_click_privacy_consent(page, messages: List[str]) -> bool:
    labels = [
        "I acknowledge", "I agree", "Privacy Policy", "Candidate Privacy",
        "Política de Privacidade", "GDPR", "Termos"
    ]
    ok = False
    for txt in labels:
        try:
            el = page.get_by_label(txt)
            await el.check(timeout=1000)
            ok = True
        except Exception:
            continue
    if ok:
        log_message(messages, "✓ Consent/Privacy marcado")
    return ok

async def try_recaptcha_checkbox(page, messages: List[str]) -> bool:
    try:
        frame = page.frame_locator("iframe[title*='reCAPTCHA'], iframe[src*='recaptcha']")
        box = frame.locator("span#recaptcha-anchor, div.recaptcha-checkbox-border").first
        await box.wait_for(state="visible", timeout=2000)
        await box.click()
        log_message(messages, "✓ reCAPTCHA checkbox clicado")
        await asyncio.sleep(1.5)
        return True
    except Exception:
        return False

async def solve_captcha(page, messages: List[str]) -> bool:
    """
    Tenta resolver CAPTCHA (reCAPTCHA v2 ou hCaptcha) usando serviço 2captcha.com
    Requer TWOCAPTCHA_API_KEY como variável de ambiente
    """
    if not TWOCAPTCHA_AVAILABLE:
        log_message(messages, "⚠️ Biblioteca 2captcha-python não disponível")
        return False
    
    try:
        # Detectar tipo de CAPTCHA
        captcha_type = None
        site_key = None
        
        # Verificar hCaptcha primeiro
        try:
            site_key = await page.evaluate("""
                () => {
                    const hcaptchaDiv = document.querySelector('[data-sitekey]');
                    if (hcaptchaDiv) {
                        const iframe = document.querySelector('iframe[src*="hcaptcha"]');
                        if (iframe) return { type: 'hcaptcha', key: hcaptchaDiv.getAttribute('data-sitekey') };
                    }
                    return null;
                }
            """)
            if site_key:
                captcha_type = "hcaptcha"
                site_key = site_key.get('key') if isinstance(site_key, dict) else site_key
                log_message(messages, f"🔍 Detectado hCaptcha (site key: {site_key[:20] if site_key else ''}...)")
        except:
            pass
        
        # Verificar reCAPTCHA v2
        if not captcha_type:
            try:
                site_key = await page.evaluate("""
                    () => {
                        const iframe = document.querySelector('iframe[src*="recaptcha"]');
                        if (!iframe) return null;
                        const src = iframe.getAttribute('src');
                        const match = src.match(/[?&]k=([^&]+)/);
                        return match ? match[1] : null;
                    }
                """)
                if site_key:
                    captcha_type = "recaptcha"
                    log_message(messages, f"🔍 Detectado reCAPTCHA v2 (site key: {site_key[:20]}...)")
            except:
                pass
        
        if not captcha_type or not site_key:
            log_message(messages, "Nenhum CAPTCHA detectado na página")
            return False
        
        # Obter API key do 2captcha
        twocaptcha_key = os.getenv("TWOCAPTCHA_API_KEY")
        if not twocaptcha_key:
            log_message(messages, "⚠️ TWOCAPTCHA_API_KEY não configurada - pulando resolução de CAPTCHA")
            return False
        
        log_message(messages, f"🔓 Resolvendo {captcha_type.upper()}...")
        
        # Resolver CAPTCHA usando 2captcha.com
        solver = TwoCaptcha(twocaptcha_key)
        
        if captcha_type == "hcaptcha":
            result = solver.hcaptcha(
                sitekey=site_key,
                url=page.url
            )
        else:  # recaptcha
            result = solver.recaptcha(
                sitekey=site_key,
                url=page.url
            )
        
        response_token = result.get('code')
        
        if response_token:
            # Injetar token na página
            if captcha_type == "hcaptcha":
                await page.evaluate(f"""
                    (token) => {{
                        const textarea = document.querySelector('[name="h-captcha-response"]');
                        if (textarea) textarea.innerHTML = token;
                        
                        // Try to trigger hCaptcha callback
                        if (window.hcaptcha) {{
                            try {{
                                const widgets = document.querySelectorAll('.h-captcha');
                                widgets.forEach((widget) => {{
                                    const widgetId = widget.dataset.hcaptchaWidgetId;
                                    if (widgetId && window.hcaptcha.setResponse) {{
                                        window.hcaptcha.setResponse(widgetId, token);
                                    }}
                                }});
                            }} catch (e) {{
                                console.log('Could not trigger hCaptcha callback:', e);
                            }}
                        }}
                    }}
                """, response_token)
            else:  # recaptcha
                await page.evaluate(f"""
                    (token) => {{
                        const textarea = document.getElementById('g-recaptcha-response');
                        if (textarea) textarea.innerHTML = token;
                        if (typeof grecaptcha !== 'undefined') {{
                            grecaptcha.getResponse = function() {{ return token; }};
                        }}
                    }}
                """, response_token)
            
            log_message(messages, f"✓ {captcha_type.upper()} resolvido com sucesso")
            await asyncio.sleep(2)  # Dar tempo para o site processar
            return True
        else:
            log_message(messages, f"✗ Falha ao resolver {captcha_type.upper()}")
            return False
            
    except Exception as e:
        log_message(messages, f"✗ Erro ao resolver CAPTCHA: {e}")
        return False

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
                            "content": """You are an AI that analyzes job application screenshots. Return STRICT JSON (no markdown fences).

FORMAT:
{
  "success": true/false,
  "reason": "explanation",
  "instructions": [
    {"action": "fill", "selector": "Field Label Text", "value": "derived from CV"},
    {"action": "select", "selector": "Dropdown Label", "value": "Yes/No"},
    {"action": "check", "selector": "Checkbox Label"}
  ],
  "captcha_type": "iframe" (if present)
}

RULES:
- Use EXACT label text visible on form for "selector"
- Derive all values from CV when fields empty
- actions: fill, select, check, click
- Always infer job_title, legal_name, city from CV"""
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


async def execute_vision_instructions(page, instructions: List[object], messages: List[str]) -> bool:
    """
    Executa as instruções fornecidas pelo Vision.
    Suporta tanto strings como objetos {action, selector, value} e prioriza seletores por label.
    """
    if not instructions:
        return False

    log_message(messages, f"🔧 Executando {len(instructions)} instruções do Vision...")
    executed_count = 0

    async def fill_by_label(label_text: str, value: str) -> bool:
        try:
            el = page.get_by_label(label_text)
            await el.wait_for(state="visible", timeout=3000)
            try:
                await el.fill(str(value))
                return True
            except Exception:
                # Tentar como <select>
                try:
                    await el.select_option(label=str(value))
                    return True
                except Exception:
                    try:
                        await el.select_option(str(value))
                        return True
                    except Exception:
                        return False
        except Exception:
            return False

    async def check_by_label(label_text: str) -> bool:
        try:
            await page.get_by_label(label_text).check(timeout=3000)
            return True
        except Exception:
            return False

    async def click_by_name(name: str) -> bool:
        try:
            await page.get_by_role("button", name=name).first.click(timeout=3000)
            return True
        except Exception:
            try:
                await page.get_by_role("link", name=name).first.click(timeout=3000)
                return True
            except Exception:
                try:
                    await page.locator(f"button:has-text('{name}'), a:has-text('{name}')").first.click(timeout=3000)
                    return True
                except Exception:
                    return False

    for i, instruction in enumerate(instructions, 1):
        try:
            # Instrução como dict estruturado
            if isinstance(instruction, dict):
                action = str(instruction.get("action", "")).lower()
                selector = instruction.get("selector") or instruction.get("field") or ""
                value = instruction.get("value") or instruction.get("answer") or ""
                log_message(messages, f"  [{i}] Executando: {instruction}")

                if action in ["fill", "type"] and selector:
                    # 1) tentar por label
                    if await fill_by_label(selector, value):
                        log_message(messages, f"    ✓ Preenchido por label: {selector} -> {value}")
                        executed_count += 1
                        await asyncio.sleep(random.uniform(0.4, 0.8))
                        continue
                    # 2) fallback: CSS direto
                    try:
                        if await fill_field(page, str(selector), str(value), messages):
                            executed_count += 1
                            await asyncio.sleep(random.uniform(0.4, 0.8))
                            continue
                    except Exception:
                        pass

                if action in ["select", "choose"] and selector:
                    ok_sel = False
                    # 1) label
                    try:
                        el = page.get_by_label(selector)
                        await el.select_option(label=str(value))
                        ok_sel = True
                    except Exception:
                        try:
                            el = page.get_by_label(selector)
                            await el.select_option(str(value))
                            ok_sel = True
                        except Exception:
                            try:
                                await page.locator(str(selector)).first.select_option(label=str(value))
                                ok_sel = True
                            except Exception:
                                ok_sel = False
                    if ok_sel:
                        log_message(messages, f"    ✓ Dropdown por label: {selector} = {value}")
                        executed_count += 1
                        await asyncio.sleep(random.uniform(0.4, 0.8))
                        continue

                if action in ["check", "tick"] and selector:
                    if await check_by_label(selector):
                        log_message(messages, f"    ✓ Marcado por label: {selector}")
                        executed_count += 1
                        await asyncio.sleep(random.uniform(0.3, 0.6))
                        continue
                    try:
                        await page.locator(str(selector)).first.check(timeout=3000)
                        log_message(messages, f"    ✓ Marcado: {selector}")
                        executed_count += 1
                        await asyncio.sleep(random.uniform(0.3, 0.6))
                        continue
                    except Exception:
                        pass

                if action in ["click", "press"]:
                    target = str(selector or value)
                    if await click_by_name(target):
                        log_message(messages, f"    ✓ Click por nome: {target}")
                        executed_count += 1
                        await asyncio.sleep(random.uniform(0.3, 0.6))
                        continue
                    try:
                        await page.locator(target).first.click(timeout=3000)
                        log_message(messages, f"    ✓ Click por seletor: {target}")
                        executed_count += 1
                        await asyncio.sleep(random.uniform(0.3, 0.6))
                        continue
                    except Exception:
                        pass

            # Instrução como string (compat)
            text = str(instruction)
            # Skip unsolvable iframe CAPTCHAs only
            if "UNSOLVABLE_IFRAME" in text:
                log_message(messages, f"  [{i}] ⚠ CAPTCHA iframe não pode ser resolvido - a pular")
                continue

            log_message(messages, f"  [{i}] Executando: {text}")
            lower = text.lower()

            # CAPTCHA image grid
            if "click captcha image at position" in lower:
                match = re.search(r"position\s*\((\d+),\s*(\d+)\)", text)
                if match:
                    row, col = int(match.group(1)), int(match.group(2))
                    try:
                        cols_per_row = 3
                        image_index = (row - 1) * cols_per_row + col
                        selectors = [
                            f".captcha-grid img:nth-child({image_index})",
                            f"[class*='captcha'] img:nth-child({image_index})",
                            f"img[alt*='captcha']:nth-child({image_index})",
                            f".rc-imageselect-tile:nth-child({image_index})",
                        ]
                        clicked = False
                        for sel in selectors:
                            try:
                                el = page.locator(sel).first
                                if await el.count() > 0:
                                    await el.click(timeout=2000)
                                    log_message(messages, f"    ✓ Clicou CAPTCHA ({row},{col})")
                                    clicked = True
                                    executed_count += 1
                                    break
                            except Exception:
                                continue
                        if not clicked:
                            all_imgs = page.locator("img")
                            count = await all_imgs.count()
                            if image_index <= count:
                                await all_imgs.nth(image_index - 1).click(timeout=2000)
                                log_message(messages, f"    ✓ Clicou CAPTCHA ({row},{col}) via fallback")
                                executed_count += 1
                    except Exception as e:
                        log_message(messages, f"    ✗ Falha ao clicar em CAPTCHA image: {e}")
                continue

            if "click captcha submit" in lower:
                try:
                    for sel in [
                        "button:has-text('Submit')",
                        "button:has-text('Verify')",
                        "[class*='captcha'] button[type='submit']",
                        ".captcha-submit",
                        "#captcha-submit",
                    ]:
                        try:
                            btn = page.locator(sel).first
                            if await btn.is_visible(timeout=2000):
                                await btn.click()
                                log_message(messages, f"    ✓ Clicou submit CAPTCHA")
                                executed_count += 1
                                break
                        except Exception:
                            continue
                except Exception as e:
                    log_message(messages, f"    ✗ Falha ao clicar submit CAPTCHA: {e}")
                continue

            if "select option" in lower and "dropdown" in lower:
                match = re.search(r"select option ['\"](.+?)['\"] in dropdown\s*(?:\[name=['\"](.+?)['\"]\]|['\"](.+?)['\"])", text, re.IGNORECASE)
                if match:
                    option_value = match.group(1)
                    dropdown_name = match.group(2) or match.group(3)
                    try:
                        try:
                            await page.get_by_label(dropdown_name).select_option(label=option_value)
                        except Exception:
                            await page.locator(f"select[name='{dropdown_name}']").first.select_option(label=option_value)
                        log_message(messages, f"    ✓ Dropdown selecionado: {option_value}")
                        executed_count += 1
                    except Exception as e:
                        log_message(messages, f"    ✗ Falha ao selecionar dropdown: {e}")

            elif "fill" in lower:
                match = re.search(r"fill\s+(.+?)\s+with\s+(?:value\s+)?['\"](.+?)['\"]", text, re.IGNORECASE)
                if match:
                    selector_or_label, value = match.groups()
                    # se tiver aspas, assumir label
                    quoted = re.search(r"['\"](.+?)['\"]", selector_or_label)
                    if quoted:
                        if await fill_by_label(quoted.group(1), value):
                            log_message(messages, f"    ✓ Preenchido por label: {quoted.group(1)}")
                            executed_count += 1
                    else:
                        if await fill_field(page, selector_or_label.strip(), value.strip(), messages):
                            executed_count += 1

            elif "click" in lower:
                match = re.search(r"click\s+(.+)", text, re.IGNORECASE)
                if match:
                    target = match.group(1).strip()
                    quoted = re.search(r"['\"](.+?)['\"]", target)
                    if quoted:
                        if await click_by_name(quoted.group(1)):
                            executed_count += 1
                    else:
                        try:
                            await page.locator(target).first.click(timeout=3000)
                            log_message(messages, f"    ✓ Clicou em: {target[:40]}")
                            executed_count += 1
                        except Exception as e:
                            log_message(messages, f"    ✗ Falha ao clicar: {e}")

            elif "select" in lower:
                match = re.search(r"select\s+(?:option\s+)?['\"](.+?)['\"]\s+in\s+(.+)", text, re.IGNORECASE)
                if match:
                    value, selector = match.groups()
                    try:
                        await page.locator(selector.strip()).select_option(value.strip())
                        log_message(messages, f"    ✓ Selecionou: {value}")
                        executed_count += 1
                    except Exception as e:
                        log_message(messages, f"    ✗ Falha ao selecionar: {e}")

            elif "check" in lower:
                match = re.search(r"check\s+(.+)", text, re.IGNORECASE)
                if match:
                    selector = match.group(1).strip()
                    quoted = re.search(r"['\"](.+?)['\"]", selector)
                    if quoted:
                        if await check_by_label(quoted.group(1)):
                            log_message(messages, f"    ✓ Marcou checkbox por label: {quoted.group(1)}")
                            executed_count += 1
                    else:
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
    try:
        await page.wait_for_timeout(1200)
        html = (await page.content()).lower()

        # Sinais de erro/pendência prevalecem
        error_hits = [
            "please fill out this field", "required", "fix the errors", "invalid"
        ]
        if any(h in html for h in error_hits):
            log_message(messages, "⚠ Mensagens de erro/required ainda presentes")
            return False

        # Sinais de sucesso explícitos
        if any(h in html for h in SUCCESS_HINTS):
            log_message(messages, "✓ Texto de sucesso detectado")
            return True

        # URL mudou? Só conta se nova página tiver confirmação de sucesso
        try:
            await page.wait_for_url(lambda u: u != job_url, timeout=2000)
            new_html = (await page.content()).lower()
            if any(h in new_html for h in SUCCESS_HINTS):
                log_message(messages, "✓ Confirmação de sucesso após redirect")
                return True
        except PwTimeout:
            pass
    except Exception as e:
        log_message(messages, f"⚠ Erro ao detectar sucesso: {e}")
    return False

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
            # Argumentos anti-detecção de bot e CAPTCHA
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-web-security",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--disable-infobars",
                    "--window-size=1920,1080",
                    "--start-maximized",
                    "--disable-gpu"
                ]
            )
            
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            
            page = await context.new_page()
            page.set_default_timeout(15000)
            
            # Remover propriedades que indicam automação
            await page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
                
                window.chrome = {
                    runtime: {}
                };
                
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5]
                });
                
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en']
                });
            """)

            log_message(messages, f"Iniciando candidatura: {job_url}")
            await page.goto(job_url, wait_until="domcontentloaded")
            await asyncio.sleep(1.2)
            await try_open_apply_modal(page, messages)
            if pdf_bytes:
                await upload_resume(page, pdf_bytes, messages)

            # Expandir secções colapsadas primeiro
            await expand_collapsed_sections(page, messages)

            # Preenchimento por label (mais resiliente)
            await fill_by_possible_labels(page, ["Full name", "Name", "Nome completo"], user_data.get("full_name", ""), messages)
            await fill_by_possible_labels(page, ["Email", "E-mail"], user_data.get("email", ""), messages)
            await fill_by_possible_labels(page, ["Phone", "Mobile", "Telefone"], user_data.get("phone", ""), messages)

            filled_name = await fill_field(page, SELECTORS["full_name"], user_data.get("full_name", ""), messages)
            if not filled_name and user_data.get("full_name"):
                parts = user_data["full_name"].split(maxsplit=1)
                first = parts[0]
                last = parts[1] if len(parts) > 1 else ""
                await fill_field(page, SELECTORS["first_name"], first, messages)
                await fill_field(page, SELECTORS["last_name"], last, messages)

            await fill_field(page, SELECTORS["email"], user_data.get("email", ""), messages)
            await fill_field(page, SELECTORS["phone"], user_data.get("phone", ""), messages)

            # Location com autocomplete inteligente
            loc_val = user_data.get("location") or user_data.get("current_location", "")
            if loc_val:
                # Tenta método específico para autocomplete primeiro
                if not await fill_autocomplete_location(page, loc_val, messages):
                    # Fallback para label
                    if not await fill_by_possible_labels(page, ["Location", "City", "Location (City)"], loc_val, messages):
                        # Fallback para selector CSS
                        if not await fill_autocomplete(page, SELECTORS["location"], loc_val, messages):
                            await fill_field(page, SELECTORS["location"], loc_val, messages)

            await fill_field(page, SELECTORS["current_company"], user_data.get("current_company", ""), messages)
            cloc_val = user_data.get("current_location", "")
            if cloc_val:
                if not await fill_autocomplete(page, SELECTORS["current_location"], cloc_val, messages):
                    await fill_field(page, SELECTORS["current_location"], cloc_val, messages)

            await fill_field(page, SELECTORS["salary"], user_data.get("salary_expectations", ""), messages)
            await fill_field(page, SELECTORS["notice"], user_data.get("notice_period", ""), messages)
            await fill_field(page, SELECTORS["additional"], user_data.get("additional_info", ""), messages)

            # Verificar e corrigir campos obrigatórios
            problems = await check_required_errors(page, messages)
            if problems:
                await asyncio.sleep(0.8)
                await autofix_required_fields(page, messages)
                await asyncio.sleep(0.5)
                problems = await check_required_errors(page, messages)

            if plan_only:
                status = "planned_only"
            else:
                # Self-healing loop com Vision AI (max 5 tentativas)
                MAX_RETRIES = 5
                retry_count = 0
                
                while retry_count < MAX_RETRIES:
                    retry_count += 1
                    log_message(messages, f"🔄 Tentativa {retry_count}/{MAX_RETRIES}")

                    # Scroll para forçar render de campos lazy e expandir secções
                    try:
                        await page.evaluate("window.scrollTo(0, 0)")
                        await asyncio.sleep(0.3)
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        await asyncio.sleep(0.6)
                    except Exception:
                        pass

                    # Expandir secções colapsadas (se ainda houver)
                    await expand_collapsed_sections(page, messages)

                    # Detectar e corrigir campos obrigatórios após scroll
                    try:
                        errors = page.locator('[aria-invalid="true"], .field-error, [data-required="true"].error')
                        error_count = await errors.count()
                        if error_count > 0:
                            log_message(messages, f"⚠ Detectados {error_count} campos com erro após scroll")
                            for idx in range(min(error_count, 5)):
                                field = errors.nth(idx)
                                await field.scroll_into_view_if_needed()
                                await asyncio.sleep(0.2)
                            # Tentar autofix novamente
                            await autofix_required_fields(page, messages)
                    except Exception:
                        pass

                    # Consent/Privacy (não incluir reCAPTCHA por agora)
                    await try_click_privacy_consent(page, messages)
                    
                    # Tentar resolver CAPTCHA se presente (reCAPTCHA ou hCaptcha)
                    await solve_captcha(page, messages)

                    # Forçar HTML5 validity
                    try:
                        await page.evaluate("""
                        const f = document.querySelector('form');
                        if (f) f.reportValidity();
                        """)
                    except Exception:
                        pass

                    # Clique robusto no Submit
                    try:
                        import re as _re
                        submit_btn = page.get_by_role("button", name=_re.compile("submit", _re.I)).first
                        if await submit_btn.count() == 0:
                            submit_btn = page.locator(SELECTORS.get("submit_strict", SELECTORS["submit"]))
                        if await submit_btn.count() == 0:
                            submit_btn = page.locator(SELECTORS["submit"]).first

                        # Esperar que fique enabled e clicável
                        handle = await submit_btn.element_handle()
                        if handle:
                            await page.wait_for_function(
                                "(btn)=>!!btn && !btn.disabled && getComputedStyle(btn).pointerEvents!=='none'",
                                arg=handle,
                                timeout=4000
                            )
                            await submit_btn.scroll_into_view_if_needed()
                            if allow_submit:
                                await submit_btn.click(timeout=5000)
                                log_message(messages, "✓ Clique em Submit (robusto)")
                            else:
                                status = "awaiting_consent"
                                log_message(messages, "⚠ allow_submit=False — não submetido")
                                break
                        else:
                            log_message(messages, "✗ Botão Submit não encontrado")
                    except Exception as e:
                        log_message(messages, f"✗ Erro ao clicar Submit (robusto): {e}")

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
                    
                    # Analisar com Vision AI (com contexto do CV e dados do utilizador)
                    vision_result = await analyze_screenshot_with_vision(
                        screenshot_b64, messages, openai_api_key, user_data.get("__text"), user_data
                    )
                    
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
