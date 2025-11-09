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

# --------------------------
# Classes de Gestão
# --------------------------
class SmartRetrySystem:
    def __init__(self):
        self.retry_patterns = {
            "captcha": {"max_attempts": 3, "delay": 5},
            "network": {"max_attempts": 5, "delay": 2},
            "form_not_found": {"max_attempts": 2, "delay": 3},
            "submit": {"max_attempts": 3, "delay": 4}
        }
    async def should_retry(self, error_type: str, attempt: int, messages: List[str]) -> bool:
        pattern = self.retry_patterns.get(error_type, {"max_attempts": 2, "delay": 2})
        if attempt >= pattern["max_attempts"]:
            log_message(messages, f"❌ Máximo de tentativas ({pattern['max_attempts']}) atingido para {error_type}")
            return False
        delay = pattern["delay"]
        log_message(messages, f"🔄 Tentativa {attempt + 1}/{pattern['max_attempts']} em {delay}s...")
        await asyncio.sleep(delay)
        return True

class ApplicationState:
    def __init__(self):
        self.current_step = "initial"
        self.filled_fields = set()
        self.encountered_issues = []
        self.captcha_solved = False
        self.platform_detected = "unknown"
    def to_dict(self):
        return {
            "current_step": self.current_step,
            "filled_fields": list(self.filled_fields),
            "encountered_issues": self.encountered_issues,
            "captcha_solved": self.captcha_solved,
            "platform_detected": self.platform_detected
        }

class ApplicationLogger:
    def __init__(self):
        self.performance_metrics = {}
        self.error_stats = {}
        self.start_time = time.time()
    def log_performance(self, step: str, duration: float):
        self.performance_metrics[step] = duration
    def log_error(self, error_type: str, details: str):
        if error_type not in self.error_stats:
            self.error_stats[error_type] = 0
        self.error_stats[error_type] += 1

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
    brightdata_username: Optional[str] = None
    brightdata_password: Optional[str] = None

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
# Comportamentos Humanos
# --------------------------
async def human_mouse_movement(page, messages: List[str]):
    """Move o mouse de forma humana entre elementos"""
    try:
        viewport = page.viewport_size
        if not viewport:
            return
        points = []
        for _ in range(4):
            x = random.randint(100, viewport['width'] - 100)
            y = random.randint(100, viewport['height'] - 100)
            points.append((x, y))
        for i in range(len(points) - 1):
            start_x, start_y = points[i]
            end_x, end_y = points[i + 1]
            control_x = (start_x + end_x) // 2 + random.randint(-50, 50)
            control_y = (start_y + end_y) // 2 + random.randint(-50, 50)
            steps = random.randint(8, 15)
            for step in range(steps):
                t = step / steps
                x = (1-t)**2 * start_x + 2*(1-t)*t * control_x + t**2 * end_x
                y = (1-t)**2 * start_y + 2*(1-t)*t * control_y + t**2 * end_y
                await page.mouse.move(int(x), int(y))
                await asyncio.sleep(random.uniform(0.01, 0.03))
    except Exception:
        pass

async def human_click(page, selector: str, messages: List[str]) -> bool:
    """Clica num elemento de forma humana"""
    try:
        element = page.locator(selector).first
        await element.wait_for(state="visible", timeout=5000)
        bbox = await element.bounding_box()
        if bbox:
            target_x = bbox['x'] + bbox['width'] * random.uniform(0.3, 0.7)
            target_y = bbox['y'] + bbox['height'] * random.uniform(0.3, 0.7)
            await page.mouse.move(
                target_x + random.randint(-5, 5),
                target_y + random.randint(-5, 5)
            )
            await asyncio.sleep(random.uniform(0.1, 0.3))
            click_duration = random.uniform(50, 150)
            await page.mouse.click(target_x, target_y, delay=click_duration)
            return True
    except Exception:
        pass
    return False

async def human_type(page, selector: str, text: str, messages: List[str]) -> bool:
    """Digita texto como um humano (com erros e correções)"""
    try:
        element = page.locator(selector).first
        await element.click()
        await asyncio.sleep(random.uniform(0.2, 0.5))
        if random.random() < 0.3:
            await element.press("Control+A")
            await asyncio.sleep(random.uniform(0.1, 0.3))
            await element.press("Backspace")
            await asyncio.sleep(random.uniform(0.2, 0.4))
        for i, char in enumerate(text):
            typing_speed = random.uniform(0.08, 0.25)
            if random.random() < 0.02:
                wrong_char = random.choice('abcdefghijklmnopqrstuvwxyz')
                await element.type(wrong_char, delay=typing_speed * 1000)
                await asyncio.sleep(random.uniform(0.1, 0.3))
                await element.press("Backspace")
                await asyncio.sleep(random.uniform(0.1, 0.2))
            await element.type(char, delay=typing_speed * 1000)
            if random.random() < 0.05:
                await asyncio.sleep(random.uniform(0.5, 1.2))
        return True
    except Exception:
        pass
    return False

async def human_browsing_pattern(page, messages: List[str]):
    """Simula padrões de navegação humanos"""
    try:
        scroll_actions = random.randint(2, 4)
        for _ in range(scroll_actions):
            scroll_amount = random.randint(200, 600)
            await page.evaluate(f"""
                window.scrollBy({{
                    top: {scroll_amount},
                    behavior: 'smooth'
                }});
            """)
            await asyncio.sleep(random.uniform(0.5, 1.5))
            if random.random() < 0.3:
                await page.evaluate("""
                    window.scrollBy({
                        top: -100,
                        behavior: 'smooth'
                    });
                """)
                await asyncio.sleep(random.uniform(0.3, 0.8))
    except Exception:
        pass

async def human_reading_behavior(page, messages: List[str]):
    """Simula tempo de leitura humano"""
    try:
        content = await page.content()
        word_count = len(content.split())
        reading_time = max(1.5, min(6, word_count / 300))
        reading_time *= random.uniform(0.8, 1.3)
        await asyncio.sleep(reading_time)
    except Exception:
        pass

class HumanTiming:
    def __init__(self):
        self.think_times = {
            "simple_field": (0.3, 1.2),
            "complex_field": (0.8, 2.0),
            "decision": (1.5, 4.0),
            "review": (2.0, 5.0)
        }
    async def think(self, field_type: str = "simple_field"):
        min_time, max_time = self.think_times.get(field_type, (0.5, 1.5))
        think_time = random.uniform(min_time, max_time)
        await asyncio.sleep(think_time)
    async def random_break(self):
        if random.random() < 0.08:
            await asyncio.sleep(random.uniform(2.0, 5.0))
        elif random.random() < 0.25:
            await asyncio.sleep(random.uniform(0.5, 1.5))

# --------------------------
# Playwright helpers
# --------------------------
async def fill_field(page, selector: str, value: str, messages: List[str], human: bool = True) -> bool:
    if not value:
        return False
    try:
        loc = page.locator(selector).first
        await loc.wait_for(state="visible", timeout=8000)
        if await loc.is_visible():
            await loc.scroll_into_view_if_needed()
            if human and random.random() < 0.7:
                if await human_type(page, selector, value, messages):
                    log_message(messages, f"✓ Digitou {selector[:45]} -> '{value[:42]}'")
                    await asyncio.sleep(random.uniform(0.3, 0.9))
                    return True
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

async def fill_by_possible_labels(page, labels: List[str], value: str, messages: List[str], human: bool = True) -> bool:
    """Tenta preencher campo por vários labels possíveis com comportamento humano"""
    if not value:
        return False
    timing = HumanTiming()
    for lb in labels:
        try:
            el = page.get_by_label(lb)
            if human and random.random() < 0.6:
                await timing.think("simple_field")
            await el.fill(value, timeout=8000)
            log_message(messages, f"✓ Preenchido por label '{lb}' -> '{value[:40]}'")
            if human:
                await timing.random_break()
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

async def navigate_next_steps(page, messages: List[str], max_steps: int = 3) -> int:
    """Avança em formulários multi-etapas (Greenhouse, etc.) clicando em Next/Continue."""
    texts = [
        "Next", "Continue", "Save and continue", "Continue to application", "Proceed",
        "Próximo", "Seguinte", "Continuar"
    ]
    clicked = 0
    for _ in range(max_steps):
        found = False
        for t in texts:
            try:
                btn = page.get_by_role("button", name=re.compile(t, re.I)).first
                if await btn.is_visible(timeout=1500):
                    await btn.scroll_into_view_if_needed()
                    await btn.click(timeout=2000)
                    clicked += 1
                    log_message(messages, f"→ Clique '{t}'")
                    await asyncio.sleep(1.0)
                    found = True
                    break
            except Exception:
                continue
        if not found:
            break
    if clicked:
        log_message(messages, f"✓ Avançou {clicked} passo(s)")
    return clicked

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

async def solve_simple_text_captcha(page, messages: List[str]) -> bool:
    """Tenta resolver CAPTCHAs de texto simples"""
    try:
        text_captcha = page.locator("img[src*='captcha'], .simple-captcha, #captcha-image")
        if await text_captcha.count() > 0:
            log_message(messages, "🔍 CAPTCHA de texto simples detectado")
            # Aqui poderia usar OCR, mas por agora skip
            return False
    except Exception:
        pass
    return False

async def solve_audio_captcha(page, messages: List[str]) -> bool:
    """Tenta usar a versão áudio do CAPTCHA"""
    try:
        audio_btn = page.locator("[aria-label*='audio' i], [title*='audio' i], #recaptcha-audio-button").first
        if await audio_btn.is_visible(timeout=2000):
            log_message(messages, "🔊 Botão de áudio CAPTCHA encontrado")
            await audio_btn.click()
            await asyncio.sleep(1.5)
            # Aqui poderia fazer speech-to-text, mas por agora skip
            return False
    except Exception:
        pass
    return False

async def solve_captcha_improved(page, messages: List[str]) -> bool:
    """Versão melhorada com fallbacks"""
    log_message(messages, "🛡️ Iniciando resolução de CAPTCHA...")
    
    # 1. Tentar serviços pagos (2captcha)
    if TWOCAPTCHA_AVAILABLE and os.getenv("TWOCAPTCHA_API_KEY"):
        if await solve_captcha(page, messages):
            return True
    
    # 2. Fallback: CAPTCHAs simples
    if await solve_simple_text_captcha(page, messages):
        return True
    
    # 3. Fallback: Versão áudio
    if await solve_audio_captcha(page, messages):
        return True
    
    log_message(messages, "⚠️ Não foi possível resolver CAPTCHA automaticamente")
    return False

async def solve_captcha_brightdata(page, messages: List[str], use_brightdata: bool = False) -> bool:
    """
    Usa Bright Data Browser API para resolver CAPTCHAs automaticamente via CDP.
    Bright Data detecta e resolve automaticamente reCAPTCHA, hCaptcha, etc.
    """
    if not use_brightdata:
        log_message(messages, "⚠️ Bright Data não está ativo - usando fallback")
        return await solve_captcha_improved(page, messages)
    
    log_message(messages, "🌐 Usando Bright Data CAPTCHA Solver...")
    
    try:
        # Obter CDP session do contexto do navegador
        context = page.context
        client = await context.new_cdp_session(page)
        
        log_message(messages, "🔍 Aguardando Bright Data detectar e resolver CAPTCHA...")
        
        # Bright Data automaticamente detecta e resolve CAPTCHAs
        # Apenas aguardamos com timeout de 10 segundos para detecção
        result = await client.send('Captcha.waitForSolve', {
            'detectTimeout': 10000,  # 10 segundos para detectar
        })
        
        status = result.get('status', 'unknown')
        log_message(messages, f"📊 Status do CAPTCHA: {status}")
        
        if status == 'solve_success':
            log_message(messages, "✅ CAPTCHA resolvido automaticamente pelo Bright Data!")
            return True
        elif status == 'not_detected':
            log_message(messages, "ℹ️ Nenhum CAPTCHA detectado pela Bright Data")
            return False
        else:
            log_message(messages, f"⚠️ Status desconhecido: {status}")
            return False
            
    except Exception as e:
        log_message(messages, f"❌ Erro ao usar Bright Data CAPTCHA solver: {e}")
        log_message(messages, "🔄 Tentando fallback com 2captcha...")
        return await solve_captcha_improved(page, messages)

async def solve_captcha(page, messages: List[str]) -> bool:
    """
    Tenta resolver CAPTCHA (reCAPTCHA v2 ou hCaptcha) usando serviço 2captcha.com
    Requer TWOCAPTCHA_API_KEY como variável de ambiente
    """
    # Log IMEDIATAMENTE ao entrar na função
    log_message(messages, "=" * 50)
    log_message(messages, "🔎 [SOLVE_CAPTCHA] INICIANDO verificação de CAPTCHA...")
    log_message(messages, "=" * 50)
    
    # Verificar se biblioteca está disponível
    if not TWOCAPTCHA_AVAILABLE:
        log_message(messages, "⚠️ [ERRO] Biblioteca 2captcha-python NÃO DISPONÍVEL")
        log_message(messages, "   Instale com: pip install 2captcha-python")
        return False
    
    log_message(messages, "✓ Biblioteca 2captcha-python disponível")
    
    try:
        # Detectar tipo de CAPTCHA
        captcha_type = None
        site_key = None
        
        # Verificar hCaptcha primeiro
        log_message(messages, "🔍 [STEP 1/5] Procurando hCaptcha...")
        try:
            site_key = await page.evaluate("""
                () => {
                    // Procurar por elementos hCaptcha
                    const hcaptchaDiv = document.querySelector('[data-sitekey]');
                    const hcaptchaIframe = document.querySelector('iframe[src*="hcaptcha"]');
                    
                    if (hcaptchaDiv && hcaptchaIframe) {
                        return { type: 'hcaptcha', key: hcaptchaDiv.getAttribute('data-sitekey') };
                    }
                    
                    // Fallback: procurar por classe h-captcha
                    const hcaptchaClass = document.querySelector('.h-captcha');
                    if (hcaptchaClass) {
                        const key = hcaptchaClass.getAttribute('data-sitekey');
                        if (key) return { type: 'hcaptcha', key: key };
                    }
                    
                    return null;
                }
            """)
            if site_key:
                captcha_type = "hcaptcha"
                site_key = site_key.get('key') if isinstance(site_key, dict) else site_key
                log_message(messages, f"✅ hCaptcha DETECTADO!")
                log_message(messages, f"   Site Key: {site_key[:30]}...")
        except Exception as e:
            log_message(messages, f"⚠️ Erro ao verificar hCaptcha: {str(e)[:80]}")
        
        # Verificar reCAPTCHA v2
        if not captcha_type:
            log_message(messages, "🔍 [STEP 2/5] Procurando reCAPTCHA v2...")
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
                    log_message(messages, f"✅ reCAPTCHA v2 DETECTADO!")
                    log_message(messages, f"   Site Key: {site_key[:30]}...")
            except Exception as e:
                log_message(messages, f"⚠️ Erro ao verificar reCAPTCHA: {str(e)[:80]}")
        
        if not captcha_type or not site_key:
            log_message(messages, "✓ [RESULTADO] Nenhum CAPTCHA detectado - continuando normalmente")
            return False
        
        log_message(messages, f"🎯 [STEP 3/5] CAPTCHA CONFIRMADO: {captcha_type.upper()}")
        
        # Obter API key do 2captcha
        twocaptcha_key = os.getenv("TWOCAPTCHA_API_KEY")
        if not twocaptcha_key:
            log_message(messages, "❌ [ERRO CRÍTICO] TWOCAPTCHA_API_KEY NÃO ESTÁ CONFIGURADA!")
            log_message(messages, "   Configure a variável de ambiente no Railway:")
            log_message(messages, "   TWOCAPTCHA_API_KEY=your_api_key_here")
            return False
        
        log_message(messages, f"✓ API Key encontrada: {twocaptcha_key[:10]}...")
        log_message(messages, f"🚀 [STEP 4/5] Enviando {captcha_type.upper()} para 2captcha.com...")
        
        # Resolver CAPTCHA usando 2captcha.com
        solver = TwoCaptcha(twocaptcha_key)
        
        try:
            if captcha_type == "hcaptcha":
                log_message(messages, f"   Chamando solver.hcaptcha(sitekey={site_key[:20]}..., url={page.url[:50]}...)")
                result = solver.hcaptcha(
                    sitekey=site_key,
                    url=page.url
                )
            else:  # recaptcha
                log_message(messages, f"   Chamando solver.recaptcha(sitekey={site_key[:20]}..., url={page.url[:50]}...)")
                result = solver.recaptcha(
                    sitekey=site_key,
                    url=page.url
                )
            
            log_message(messages, f"✓ Resposta recebida do 2captcha: {str(result)[:100]}...")
        except Exception as solver_error:
            log_message(messages, f"❌ [ERRO] Falha ao chamar API 2captcha: {type(solver_error).__name__}")
            log_message(messages, f"   Mensagem: {str(solver_error)[:150]}")
            import traceback
            log_message(messages, f"   Traceback: {traceback.format_exc()[:300]}")
            return False
        
        response_token = result.get('code')
        
        if response_token:
            log_message(messages, f"✅ Token recebido: {response_token[:40]}...")
            log_message(messages, f"🔧 [STEP 5/5] Injetando token na página...")
            
            # Injetar token na página
            try:
                if captcha_type == "hcaptcha":
                    await page.evaluate(f"""
                        (token) => {{
                            console.log('[CAPTCHA] Injetando hCaptcha token...');
                            const textarea = document.querySelector('[name="h-captcha-response"]');
                            if (textarea) {{
                                textarea.innerHTML = token;
                                console.log('[CAPTCHA] Textarea preenchida');
                            }} else {{
                                console.log('[CAPTCHA] AVISO: textarea h-captcha-response não encontrada');
                            }}
                            
                            // Try to trigger hCaptcha callback
                            if (window.hcaptcha) {{
                                try {{
                                    const widgets = document.querySelectorAll('.h-captcha');
                                    console.log('[CAPTCHA] Encontrados', widgets.length, 'widgets hCaptcha');
                                    widgets.forEach((widget, i) => {{
                                        const widgetId = widget.dataset.hcaptchaWidgetId;
                                        console.log('[CAPTCHA] Widget', i, 'ID:', widgetId);
                                        if (widgetId && window.hcaptcha.setResponse) {{
                                            window.hcaptcha.setResponse(widgetId, token);
                                            console.log('[CAPTCHA] setResponse chamado para widget', widgetId);
                                        }}
                                    }});
                                }} catch (e) {{
                                    console.log('[CAPTCHA] Erro ao trigger callback:', e);
                                }}
                            }} else {{
                                console.log('[CAPTCHA] AVISO: window.hcaptcha não disponível');
                            }}
                        }}
                    """, response_token)
                    log_message(messages, "✓ Token hCaptcha injetado (verifique console do browser)")
                else:  # recaptcha
                    await page.evaluate(f"""
                        (token) => {{
                            console.log('[CAPTCHA] Injetando reCAPTCHA token...');
                            const textarea = document.getElementById('g-recaptcha-response');
                            if (textarea) {{
                                textarea.innerHTML = token;
                                console.log('[CAPTCHA] Textarea g-recaptcha-response preenchida');
                            }} else {{
                                console.log('[CAPTCHA] AVISO: textarea g-recaptcha-response não encontrada');
                            }}
                            if (typeof grecaptcha !== 'undefined') {{
                                grecaptcha.getResponse = function() {{ return token; }};
                                console.log('[CAPTCHA] grecaptcha.getResponse sobrescrito');
                            }} else {{
                                console.log('[CAPTCHA] AVISO: grecaptcha não disponível');
                            }}
                        }}
                    """, response_token)
                    log_message(messages, "✓ Token reCAPTCHA injetado (verifique console do browser)")
            except Exception as inject_error:
                log_message(messages, f"❌ [ERRO] Falha ao injetar token: {str(inject_error)[:150]}")
                return False
            
            log_message(messages, f"🎉 {captcha_type.upper()} RESOLVIDO COM SUCESSO!")
            log_message(messages, "   Aguardando 3s para processamento...")
            await asyncio.sleep(3)  # Dar mais tempo para o site processar
            return True
        else:
            log_message(messages, f"❌ [ERRO] 2captcha retornou resposta SEM TOKEN")
            log_message(messages, f"   Resposta completa: {str(result)[:200]}")
            return False
            
    except Exception as e:
        log_message(messages, f"❌ ❌ ❌ [EXCEÇÃO CRÍTICA] ❌ ❌ ❌")
        log_message(messages, f"Tipo: {type(e).__name__}")
        log_message(messages, f"Mensagem: {str(e)[:200]}")
        import traceback
        tb = traceback.format_exc()
        log_message(messages, f"Traceback completo:")
        for line in tb.split('\n')[:15]:  # Primeiras 15 linhas
            log_message(messages, f"  {line}")
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


async def verify_application_success(page, messages: List[str]) -> bool:
    """Verifica se a aplicação foi bem sucedida"""
    try:
        html = (await page.content()).lower()
        success_indicators = [
            "thank you for applying",
            "application received",
            "successfully submitted",
            "we'll be in touch",
            "application submitted",
            "candidatura recebida",
            "obrigado"
        ]
        for indicator in success_indicators:
            if indicator in html:
                log_message(messages, f"✅ Sucesso confirmado: '{indicator}'")
                return True
        url = page.url.lower()
        if "success" in url or "confirmation" in url or "thank" in url:
            log_message(messages, "✅ Sucesso confirmado via URL")
            return True
    except Exception as e:
        log_message(messages, f"⚠️ Erro ao verificar sucesso: {e}")
    return False

async def handle_platform_specific_fields(page, platform: str, user_data: Dict, messages: List[str]):
    """Campos específicos por plataforma"""
    if platform == "greenhouse":
        try:
            hear_about = page.locator("input[name*='hear' i], select[name*='hear' i]").first
            if await hear_about.count() > 0:
                field_type = await hear_about.get_attribute("type")
                if field_type == "text":
                    await hear_about.fill("LinkedIn")
                else:
                    try:
                        await hear_about.select_option(label="LinkedIn")
                    except Exception:
                        await hear_about.select_option(index=1)
                log_message(messages, "✓ Preenchido 'How did you hear about us?'")
        except Exception:
            pass
    elif platform == "lever":
        try:
            portfolio = page.locator("input[name*='portfolio' i], input[name*='website' i]").first
            if await portfolio.count() > 0 and user_data.get("portfolio"):
                await portfolio.fill(user_data["portfolio"])
                log_message(messages, "✓ Preenchido portfolio/website")
        except Exception:
            pass

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

async def detect_application_platform(page, messages: List[str]) -> Dict:
    """Detecta automaticamente a plataforma de candidatura"""
    try:
        url = page.url.lower()
        html = await page.content()
        html_lower = html.lower()
        
        # Detectar por URL
        if "greenhouse.io" in url or "greenhouse" in html_lower:
            log_message(messages, "🎯 Plataforma detectada: GREENHOUSE")
            return {
                "platform": "greenhouse",
                "confidence": "high",
                "selectors": {
                    "first_name": "input[name='first_name']",
                    "last_name": "input[name='last_name']",
                    "email": "input[name='email']",
                    "phone": "input[name='phone']"
                }
            }
        
        if "lever.co" in url or "lever" in html_lower:
            log_message(messages, "🎯 Plataforma detectada: LEVER")
            return {
                "platform": "lever",
                "confidence": "high",
                "selectors": {
                    "name": "input[name='name']",
                    "email": "input[name='email']",
                    "phone": "input[name='phone']"
                }
            }
        
        if "workday.com" in url or "workday" in html_lower:
            log_message(messages, "🎯 Plataforma detectada: WORKDAY")
            return {
                "platform": "workday",
                "confidence": "high",
                "selectors": {}
            }
        
        if "ashbyhq.com" in url or "ashby" in html_lower:
            log_message(messages, "🎯 Plataforma detectada: ASHBY")
            return {
                "platform": "ashby",
                "confidence": "high",
                "selectors": SELECTORS
            }
        
        # Genérica por padrões HTML
        if "[data-qa='application-form']" in html or "application_form" in html:
            log_message(messages, "🎯 Plataforma detectada: GREENHOUSE (por HTML)")
            return {"platform": "greenhouse", "confidence": "medium", "selectors": SELECTORS}
        
        log_message(messages, "⚠️ Plataforma não identificada - usando abordagem genérica")
        return {
            "platform": "generic",
            "confidence": "low",
            "selectors": SELECTORS
        }
        
    except Exception as e:
        log_message(messages, f"⚠️ Erro ao detectar plataforma: {e}")
        return {
            "platform": "unknown",
            "confidence": "none",
            "selectors": SELECTORS
        }

# --------------------------
# Core
# --------------------------
async def apply_to_job_async(user_data: Dict[str, str]) -> Dict:
    messages: List[str] = []
    app_state = ApplicationState()
    app_logger = ApplicationLogger()
    retry_system = SmartRetrySystem()  # Instanciar retry_system aqui
    t0 = time.time()
    job_url = user_data.get("job_url", "")
    plan_only = bool(user_data.get("plan_only", False))
    allow_submit = bool(user_data.get("allow_submit", True))
    openai_api_key = user_data.get("openai_api_key")
    
    # Inicializar variáveis de screenshot e estado
    screenshot_b64 = ""
    pre_submit_b64 = ""
    post_submit_b64 = ""
    ok = False
    status = "unknown"
    inspect_url = None

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

    # Bright Data Browser API credentials - tentar do payload primeiro, depois env vars
    brightdata_username = user_data.get("brightdata_username") or os.getenv("BRIGHTDATA_USERNAME")
    brightdata_password = user_data.get("brightdata_password") or os.getenv("BRIGHTDATA_PASSWORD")
    
    use_brightdata = brightdata_username and brightdata_password

    try:
        async with async_playwright() as p:
            # Usar Bright Data Browser API se credenciais estiverem disponíveis
            if use_brightdata:
                log_message(messages, "🌐 Conectando via Bright Data Browser API...")
                log_message(messages, f"   Username: {brightdata_username[:20]}...")
                browser_endpoint = f"wss://{brightdata_username}:{brightdata_password}@brd.superproxy.io:9222"
                
                # Retry logic para conexão ao Bright Data
                browser = None
                last_err = None
                for attempt in range(3):
                    try:
                        log_message(messages, f"   Tentativa {attempt + 1}/3...")
                        browser = await p.chromium.connect_over_cdp(
                            browser_endpoint,
                            timeout=90000  # 90s
                        )
                        log_message(messages, "✅ Conectado ao Bright Data Browser API")
                        break
                    except Exception as e:
                        last_err = e
                        log_message(messages, f"⚠️ Tentativa {attempt + 1} falhou: {str(e)[:100]}")
                        if attempt < 2:
                            log_message(messages, "   ⏳ A aguardar antes de nova tentativa...")
                            await asyncio.sleep(3)
                
                if not browser:
                    raise last_err or Exception("Falha ao conectar ao Bright Data após 3 tentativas")
                
                try:
                    log_message(messages, "   • CAPTCHA solving automático ativado")
                    log_message(messages, "   • Proxy residencial ativado")
                    log_message(messages, "   • Anti-bot evasion ativado")
                    
                    # Bright Data não fornece inspect URL via API
                    # As sessões podem ser monitorizadas em: https://brightdata.com/cp/zones
                    log_message(messages, "🌐 Bright Data proxy ativado")
                    log_message(messages, f"   📡 Proxy endpoint: {brightdata_username.split('-')[0] if brightdata_username else 'unknown'}")
                    log_message(messages, "   ℹ️ Monitorize sessões em: https://brightdata.com/cp/zones > Event log")
                    log_message(messages, "   ⏰ Nota: Pode demorar alguns segundos até aparecer no dashboard")
                    
                    context = await browser.new_context(
                        viewport={'width': 1920, 'height': 1080},
                        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                    )
                    page = await context.new_page()
                    
                    # Testar conexão Bright Data
                    log_message(messages, "🔍 A testar conexão Bright Data...")
                    try:
                        test_response = await page.goto("https://lumtest.com/myip.json", wait_until="domcontentloaded", timeout=30000)
                        if test_response and test_response.ok:
                            ip_text = await page.content()
                            log_message(messages, f"✅ Bright Data conectado! Resposta recebida")
                            # Tentar extrair IP dos dados
                            try:
                                import json
                                import re
                                json_match = re.search(r'\{[^}]+\}', ip_text)
                                if json_match:
                                    ip_data = json.loads(json_match.group())
                                    log_message(messages, f"   📍 IP: {ip_data.get('ip', 'unknown')}")
                                    log_message(messages, f"   🌍 País: {ip_data.get('country', 'unknown')}")
                            except:
                                log_message(messages, "   ✅ Conexão verificada (detalhes não disponíveis)")
                        else:
                            log_message(messages, "⚠️ Resposta inesperada ao testar Bright Data")
                    except Exception as test_err:
                        log_message(messages, f"⚠️ Erro ao testar Bright Data: {test_err}")
                        log_message(messages, "   Continuando mesmo assim...")
                        
                except Exception as e:
                    log_message(messages, f"❌ Falha ao conectar Bright Data: {e}")
                    log_message(messages, "🔄 Usando Playwright local como fallback...")
                    use_brightdata = False
            
            if not use_brightdata:
                # Argumentos anti-detecção de bot e CAPTCHA (fallback local)
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
            step_start = time.time()
            # Navegação resiliente: evita loops de redirecionamento (Ashby às vezes causa "navigate limit reached")
            nav_ok = False
            last_err = None
            for wait_state in ["commit", "domcontentloaded", "load"]:
                try:
                    await page.goto(job_url, wait_until=wait_state, timeout=60000)
                    nav_ok = True
                    log_message(messages, f"✓ Página carregada com wait_until='{wait_state}'")
                    break
                except Exception as e:
                    last_err = e
                    msg = str(e)
                    if "navigate limit" in msg.lower() or "Page.navigate" in msg:
                        log_message(messages, f"⚠️ Muitos redirecionamentos ({wait_state}). A tentar fallback...")
                        # Pequena pausa antes do próximo modo
                        await asyncio.sleep(0.5)
                        continue
                    else:
                        log_message(messages, f"⚠️ Erro ao navegar ({wait_state}): {e}")
                        await asyncio.sleep(0.5)
                        continue
            if not nav_ok:
                raise last_err or Exception("Falha ao navegar para a vaga")
            app_logger.log_performance("page_load", time.time() - step_start)
            app_state.current_step = "page_loaded"
            
            # 🎯 Detectar plataforma
            step_start = time.time()
            platform_info = await detect_application_platform(page, messages)
            app_state.platform_detected = platform_info["platform"]
            app_logger.log_performance("platform_detection", time.time() - step_start)
            
            # 🎭 Comportamento humano: tempo de leitura inicial
            await human_reading_behavior(page, messages)
            await human_browsing_pattern(page, messages)
            
            timing = HumanTiming()
            await timing.think("review")
            
            await try_open_apply_modal(page, messages)
            app_state.current_step = "form_opened"
            if pdf_bytes:
                step_start = time.time()
                await upload_resume(page, pdf_bytes, messages)
                app_logger.log_performance("cv_upload", time.time() - step_start)
                app_state.filled_fields.add("resume")

            # Expandir secções colapsadas primeiro
            await expand_collapsed_sections(page, messages)
            app_state.current_step = "filling_form"
            
            # 🎭 Movimento de mouse humano
            await human_mouse_movement(page, messages)

            # Preenchimento por label com comportamento humano
            step_start = time.time()
            if await fill_by_possible_labels(page, ["Full name", "Name", "Nome completo"], user_data.get("full_name", ""), messages, human=True):
                app_state.filled_fields.add("full_name")
            if await fill_by_possible_labels(page, ["Email", "E-mail"], user_data.get("email", ""), messages, human=True):
                app_state.filled_fields.add("email")
            if await fill_by_possible_labels(page, ["Phone", "Mobile", "Telefone"], user_data.get("phone", ""), messages, human=True):
                app_state.filled_fields.add("phone")
            app_logger.log_performance("basic_fields", time.time() - step_start)

            filled_name = await fill_field(page, SELECTORS["full_name"], user_data.get("full_name", ""), messages)
            if not filled_name and user_data.get("full_name"):
                parts = user_data["full_name"].split(maxsplit=1)
                first = parts[0]
                last = parts[1] if len(parts) > 1 else ""
                await fill_field(page, SELECTORS["first_name"], first, messages)
                await fill_field(page, SELECTORS["last_name"], last, messages)

            await fill_field(page, SELECTORS["email"], user_data.get("email", ""), messages)
            await fill_field(page, SELECTORS["phone"], user_data.get("phone", ""), messages)

            # Location com autocomplete inteligente e comportamento humano
            loc_val = user_data.get("location") or user_data.get("current_location", "")
            if loc_val:
                timing = HumanTiming()
                await timing.think("complex_field")
                if not await fill_autocomplete_location(page, loc_val, messages):
                    if not await fill_by_possible_labels(page, ["Location", "City", "Location (City)"], loc_val, messages, human=True):
                        if not await fill_autocomplete(page, SELECTORS["location"], loc_val, messages):
                            await fill_field(page, SELECTORS["location"], loc_val, messages, human=True)

            # Empresa atual com comportamento humano
            timing = HumanTiming()
            await timing.think("complex_field")
            if not await fill_by_possible_labels(page, ["Current company", "Company", "Empresa atual"], user_data.get("current_company", ""), messages, human=True):
                await fill_field(page, SELECTORS["current_company"], user_data.get("current_company", ""), messages, human=True)

            # Localização atual
            cloc_val = user_data.get("current_location", "")
            if cloc_val:
                await timing.random_break()
                if not await fill_by_possible_labels(page, ["Current location", "City", "Cidade"], cloc_val, messages, human=True):
                    if not await fill_autocomplete(page, SELECTORS["current_location"], cloc_val, messages):
                        await fill_field(page, SELECTORS["current_location"], cloc_val, messages, human=True)

            # Expectativas salariais
            await timing.think("decision")
            if not await fill_by_possible_labels(page, ["Salary", "Salary expectations", "Compensation", "Desired salary"], user_data.get("salary_expectations", ""), messages, human=True):
                await fill_field(page, SELECTORS["salary"], user_data.get("salary_expectations", ""), messages, human=True)

            # Período de aviso / disponibilidade
            await timing.random_break()
            if not await fill_by_possible_labels(page, ["Notice period", "Availability", "Earliest start date", "Disponibilidade"], user_data.get("notice_period", ""), messages, human=True):
                await fill_field(page, SELECTORS["notice"], user_data.get("notice_period", ""), messages, human=True)

            # Informação adicional / carta de apresentação
            await timing.think("decision")
            if not await fill_by_possible_labels(page, ["Additional information", "Cover letter", "Notes", "Message", "Informação adicional"], user_data.get("additional_info", ""), messages, human=True):
                await fill_field(page, SELECTORS["additional"], user_data.get("additional_info", ""), messages, human=True)
            
            # Campos específicos da plataforma
            await handle_platform_specific_fields(page, app_state.platform_detected, user_data, messages)

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
                    
                    # Resolver CAPTCHA usando Bright Data se disponível, senão fallback
                    captcha_attempt = 0
                    while captcha_attempt < 3:
                        if await solve_captcha_brightdata(page, messages, use_brightdata):
                            app_state.captcha_solved = True
                            log_message(messages, "✅ CAPTCHA resolvido")
                            break
                        captcha_attempt += 1
                        if not await retry_system.should_retry("captcha", captcha_attempt, messages):
                            break


                    # Forçar HTML5 validity
                    try:
                        await page.evaluate("""
                        const f = document.querySelector('form');
                        if (f) f.reportValidity();
                        """)
                    except Exception:
                        pass

                    # Em formulários multi-etapas, avançar antes de procurar Submit
                    try:
                        await navigate_next_steps(page, messages, max_steps=3)
                    except Exception:
                        pass
                    
                    # 🎭 Scroll final para rever formulário (comportamento humano)
                    try:
                        await page.evaluate("window.scrollTo(0, 0)")
                        await asyncio.sleep(random.uniform(0.8, 2.0))
                        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        timing = HumanTiming()
                        await timing.think("review")
                    except Exception:
                        pass

                    # Clique robusto no Submit com comportamento humano
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
                            
                            # 📸 Screenshot PRE-SUBMIT
                            pre_submit_b64 = ""
                            try:
                                pre_png = await page.screenshot(full_page=True)
                                pre_submit_b64 = base64.b64encode(pre_png).decode("utf-8")
                                log_message(messages, "✓ Screenshot pré-submit capturado")
                            except Exception as e:
                                log_message(messages, f"⚠ Não foi possível capturar pré-submit: {e}")
                            
                            if allow_submit:
                                # 🎭 Clique humano no submit
                                timing = HumanTiming()
                                await timing.think("review")
                                
                                # Tentar clique humano primeiro
                                submit_selector = f"button:has-text('Submit')"
                                if not await human_click(page, submit_selector, messages):
                                    # Fallback para clique normal
                                    await submit_btn.click(timeout=5000)
                                
                                log_message(messages, "✓ Clique em Submit (humano)")
                            else:
                                status = "awaiting_consent"
                                log_message(messages, "⚠ allow_submit=False — não submetido")
                                break
                        else:
                            log_message(messages, "✗ Botão Submit não encontrado")
                    except Exception as e:
                        log_message(messages, f"✗ Erro ao clicar Submit (robusto): {e}")
                        app_logger.log_error("submit_click", str(e))
                        app_state.encountered_issues.append(f"submit_error: {str(e)}")

                    await asyncio.sleep(2.0)
                    app_state.current_step = "submitted"
                    
                    # 📸 Screenshot POST-SUBMIT
                    post_submit_b64 = ""
                    try:
                        post_png = await page.screenshot(full_page=True)
                        post_submit_b64 = base64.b64encode(post_png).decode("utf-8")
                        screenshot_b64 = post_submit_b64  # manter compatibilidade
                        log_message(messages, "✓ Screenshot pós-submit capturado")
                    except Exception as e:
                        log_message(messages, f"✗ Erro ao capturar screenshot pós-submit: {e}")
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

    # Calcular métricas finais
    total_time = time.time() - app_logger.start_time
    app_logger.log_performance("total_execution", total_time)
    
    elapsed = round(time.time() - t0, 2)
    
    # Garantir que temos as variáveis de screenshot
    if 'pre_submit_b64' not in locals():
        pre_submit_b64 = ""
    if 'post_submit_b64' not in locals():
        post_submit_b64 = screenshot_b64 if screenshot_b64 else ""
    
    return {
        "ok": ok,
        "status": status,
        "job_url": job_url,
        "elapsed_s": elapsed,
        "platform": app_state.platform_detected or None,
        "log": messages,
        "screenshot": (post_submit_b64 or screenshot_b64 or ""),  # compat
        "inspect_url": inspect_url,  # URL para ver browser em tempo real
        "evidence": {
            "pre_submit_screenshot_b64": pre_submit_b64 or "",
            "post_submit_screenshot_b64": post_submit_b64 or screenshot_b64 or ""
        },
        "state": app_state.to_dict(),
        "metrics": app_logger.performance_metrics,
        "errors": app_logger.error_stats
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

@app.get("/healthz")
def healthz():
    return {"status": "ok"}

@app.post("/apply")
async def auto_apply(req: ApplyRequest):
    try:
        logger.info(f"📥 Recebendo request para job: {req.job_url}")
        logger.info(f"📋 Dados: name={req.full_name}, email={req.email}, phone={req.phone}")
        
        result = await apply_to_job_async(req.dict())
        
        logger.info(f"✅ Resultado: status={result.get('status')}, ok={result.get('ok')}")
        
        # Normalizar resposta no formato esperado
        payload = {
            "ok": bool(result.get("ok")),
            "status": result.get("status", "unknown"),
            "platform": result.get("platform") or (result.get("state", {}) or {}).get("platform_detected"),
            "evidence": result.get("evidence", {}),
            "log": result.get("log", []),
            "inspect_url": result.get("inspect_url"),  # URL de inspeção do browser
            "error": None
        }
        
        # Se falhou, incluir erro
        if not payload["ok"] or payload["status"] in ("error", "failed"):
            error_details = (result.get("errors") or {})
            payload["error"] = error_details.get("fatal") or result.get("error") or "Auto-apply failed"
        
        return payload
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
