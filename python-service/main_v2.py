"""
Auto-Apply Service V2 - Vision-First Architecture
Implements intelligent form filling using Vision AI:
- Screenshot-first approach with GPT-4 Vision analysis
- Automatic field detection and data extraction
- Smart data matching from CV/profile
- CAPTCHA detection and handling
- Multi-page form support
- Pre and post-submit validation
- Bright Data Browser API integration
"""

import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import random
import re
import time
import traceback
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from playwright.async_api import Browser, Page, async_playwright
from pydantic import BaseModel, Field

# ============================================================================
# LOGGING
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ============================================================================
# FASTAPI APP
# ============================================================================
app = FastAPI(title="Auto-Apply Service V2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================================
# MODELS
# ============================================================================

class UseMCP(str, Enum):
    AUTO = "auto"
    ALWAYS = "always"
    NEVER = "never"

class AutoApplyRequest(BaseModel):
    job_url: str
    candidate_id: str
    plan_only: bool = False
    allow_submit: bool = True
    use_bright_data: bool = True
    use_mcp: UseMCP = UseMCP.AUTO
    auto_captcha_allowed: bool = True
    debug_mode: bool = False  # NEW: Enable ultra-detailed logging and HTML dumps
    answers_override: Dict[str, str] = Field(default_factory=dict)
    
    # Candidate data
    full_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    current_company: str = ""
    linkedin_url: str = ""
    years_of_experience: str = ""
    resume: str = ""  # URL or base64
    
    # API keys
    openai_api_key: str = ""
    brightdata_username: str = ""
    brightdata_password: str = ""
    twocaptcha_api_key: str = ""

class AutoApplyResponse(BaseModel):
    status: str  # success, error, needs_review, verify_failed
    run_id: str
    message: str
    screenshot_pre: Optional[str] = None
    screenshot_post: Optional[str] = None
    filled_fields: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    logs: List[str] = Field(default_factory=list)
    telemetry: Dict[str, Any] = Field(default_factory=dict)

# ============================================================================
# SELECTOR ENGINE (CORE OF PLAN)
# ============================================================================

def normalize_label(text: str) -> str:
    """Remove asterisks, colons, trim, lowercase."""
    return text.replace("*", "").replace(":", "").strip().lower()

async def resolve_locator(page: Page, label_text: str):
    """
    Robust locator resolution with multiple strategies:
    1. By label (get_by_label)
    2. By placeholder (get_by_placeholder)
    3. By role + name (get_by_role)
    4. Direct input[name] attribute
    5. Direct input[id] attribute
    6. Partial text match in labels
    7. XPath near label (fallback)
    """
    normalized = normalize_label(label_text)
    logger.info(f"🔍 RESOLVING LOCATOR for: '{label_text}' → normalized: '{normalized}'")
    
    try:
        # 1. Try by label
        logger.debug(f"  Strategy 1/7: Trying get_by_label('{normalized}')")
        locator = page.get_by_label(normalized, exact=False)
        count = await locator.count()
        logger.debug(f"  → Found {count} elements by label")
        if count > 0:
            logger.info(f"✅ FOUND BY LABEL: '{normalized}' ({count} matches)")
            return locator.first
    except Exception as e:
        logger.debug(f"  → get_by_label failed: {e}")
    
    try:
        # 2. Try by placeholder
        logger.debug(f"  Strategy 2/7: Trying get_by_placeholder('{normalized}')")
        locator = page.get_by_placeholder(normalized, exact=False)
        count = await locator.count()
        logger.debug(f"  → Found {count} elements by placeholder")
        if count > 0:
            logger.info(f"✅ FOUND BY PLACEHOLDER: '{normalized}' ({count} matches)")
            return locator.first
    except Exception as e:
        logger.debug(f"  → get_by_placeholder failed: {e}")
    
    try:
        # 3. Try by role
        logger.debug(f"  Strategy 3/7: Trying get_by_role('textbox', name='{normalized}')")
        locator = page.get_by_role("textbox", name=normalized, exact=False)
        count = await locator.count()
        logger.debug(f"  → Found {count} elements by role")
        if count > 0:
            logger.info(f"✅ FOUND BY ROLE: '{normalized}' ({count} matches)")
            return locator.first
    except Exception as e:
        logger.debug(f"  → get_by_role failed: {e}")
    
    try:
        # 4. Try by name attribute (input[name*='...'])
        logger.debug(f"  Strategy 4/7: Trying input[name*='{normalized}']")
        locator = page.locator(f"input[name*='{normalized}' i], textarea[name*='{normalized}' i]")
        count = await locator.count()
        logger.debug(f"  → Found {count} elements by name attribute")
        if count > 0:
            logger.info(f"✅ FOUND BY NAME ATTR: '{normalized}' ({count} matches)")
            return locator.first
    except Exception as e:
        logger.debug(f"  → name attribute search failed: {e}")
    
    try:
        # 5. Try by id attribute (input[id*='...'])
        logger.debug(f"  Strategy 5/7: Trying input[id*='{normalized}']")
        locator = page.locator(f"input[id*='{normalized}' i], textarea[id*='{normalized}' i]")
        count = await locator.count()
        logger.debug(f"  → Found {count} elements by id attribute")
        if count > 0:
            logger.info(f"✅ FOUND BY ID ATTR: '{normalized}' ({count} matches)")
            return locator.first
    except Exception as e:
        logger.debug(f"  → id attribute search failed: {e}")
    
    try:
        # 6. Try to find label containing text and get its associated input
        logger.debug(f"  Strategy 6/7: Trying label containing '{normalized}'")
        locator = page.locator(f"label:has-text('{normalized}') + input, label:has-text('{normalized}') + textarea")
        count = await locator.count()
        logger.debug(f"  → Found {count} elements via adjacent label")
        if count > 0:
            logger.info(f"✅ FOUND BY ADJACENT LABEL: '{normalized}' ({count} matches)")
            return locator.first
    except Exception as e:
        logger.debug(f"  → adjacent label search failed: {e}")
    
    # 7. Fallback: XPath near label
    try:
        logger.debug(f"  Strategy 7/7: Trying XPath fallback")
        xpath = f'//label[contains(translate(normalize-space(.), "*:", ""), "{normalized}")]/following::*[self::input or self::textarea][1]'
        logger.debug(f"  → XPath: {xpath}")
        locator = page.locator(f"xpath={xpath}")
        count = await locator.count()
        logger.debug(f"  → Found {count} elements by XPath")
        if count > 0:
            logger.info(f"✅ FOUND BY XPATH: '{normalized}' ({count} matches)")
            return locator.first
    except Exception as e:
        logger.debug(f"  → XPath fallback failed: {e}")
    
    logger.error(f"❌ FAILED ALL STRATEGIES for: '{label_text}'")
    logger.error(f"   Tried: label, placeholder, role(textbox), name attr, id attr, adjacent label, XPath - all returned 0 matches")
    return None

async def fill_field_robust(page: Page, label: str, value: str, logs: List[str]):
    """Fill field using robust selector engine."""
    logger.info(f"🎯 ATTEMPTING TO FILL: '{label}' with value: '{value}'")
    logs.append(f"🎯 Trying to fill '{label}'...")
    
    try:
        locator = await resolve_locator(page, label)
        if locator:
            logger.info(f"✅ LOCATOR FOUND for '{label}', attempting to fill...")
            await locator.click()
            await asyncio.sleep(0.1)
            await locator.fill(value)
            await asyncio.sleep(0.2)
            
            # Verify the value was filled
            filled_value = await locator.input_value()
            if filled_value == value:
                logs.append(f"✅ Successfully filled '{label}': {value}")
                logger.info(f"✅ VERIFIED: '{label}' = '{value}'")
                return True
            else:
                logs.append(f"⚠️ Filled '{label}' but value mismatch: expected '{value}', got '{filled_value}'")
                logger.warning(f"⚠️ VALUE MISMATCH for '{label}'")
                return False
        else:
            logs.append(f"❌ Could not find field: '{label}' (tried label, placeholder, role, XPath)")
            logger.error(f"❌ LOCATOR NOT FOUND for: '{label}'")
            return False
    except Exception as e:
        logs.append(f"❌ Error filling '{label}': {str(e)}")
        logger.error(f"❌ EXCEPTION filling '{label}': {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return False

async def fill_field_with_css_fallback(page: Page, field_name: str, value: str, logs: List[str]) -> bool:
    """
    Tenta preencher campo usando:
    1. Engine atual (resolve_locator + fill_field_robust)
    2. Fallback: Seletores CSS diretos do FIELD_SELECTORS
    """
    if not value:
        logger.debug(f"⏭️ Skipping '{field_name}' - empty value")
        return False
    
    # Fase 1: Tentar engine atual
    logger.info(f"🔄 Phase 1: Trying current engine for '{field_name}'")
    success = await fill_field_robust(page, field_name, value, logs)
    if success:
        return True
    
    # Fase 2: Fallback CSS direto
    logger.warning(f"⚠️ Phase 1 failed for '{field_name}', trying CSS fallback...")
    logs.append(f"🔄 Tentando seletores CSS diretos para '{field_name}'...")
    
    if field_name not in FIELD_SELECTORS:
        logger.error(f"❌ No CSS selectors defined for '{field_name}'")
        return False
    
    selectors = FIELD_SELECTORS[field_name]
    selector_str = ", ".join(selectors)
    logger.debug(f"  CSS selectors: {selector_str}")
    
    try:
        locator = page.locator(selector_str).first
        count = await locator.count()
        logger.debug(f"  → Found {count} elements with CSS selectors")
        
        if count > 0:
            is_visible = await locator.is_visible()
            logger.debug(f"  → Visible: {is_visible}")
            
            if is_visible:
                await locator.click()
                await asyncio.sleep(0.1)
                await locator.fill(value)
                await asyncio.sleep(0.2)
                
                # Verificar
                filled_value = await locator.input_value()
                if filled_value == value:
                    logs.append(f"✅ CSS fallback success for '{field_name}': {value}")
                    logger.info(f"✅ CSS FALLBACK SUCCESS: '{field_name}' = '{value}'")
                    return True
                else:
                    logs.append(f"⚠️ CSS fallback mismatch for '{field_name}'")
                    logger.warning(f"⚠️ CSS FALLBACK MISMATCH: '{field_name}'")
                    return False
            else:
                logger.warning(f"⚠️ CSS locator not visible for '{field_name}'")
        else:
            logger.warning(f"⚠️ No CSS elements found for '{field_name}'")
    except Exception as e:
        logs.append(f"❌ CSS fallback error for '{field_name}': {str(e)}")
        logger.error(f"❌ CSS FALLBACK EXCEPTION for '{field_name}': {e}")
        return False
    
    return False

# ============================================================================
# BRIGHT DATA INTEGRATION
# ============================================================================

def get_bright_data_proxy(username: str, password: str) -> Optional[Dict[str, str]]:
    """Build Bright Data HTTP proxy config (legacy)."""
    if not username or not password:
        return None
    
    proxy_host = "brd.superproxy.io"
    proxy_port = "33335"
    
    return {
        "server": f"http://{proxy_host}:{proxy_port}",
        "username": username,
        "password": password,
    }

def get_bright_data_browser_api_endpoint(username: str, password: str) -> Optional[str]:
    """Build Bright Data Browser API WebSocket endpoint."""
    if not username or not password:
        return None
    
    # Format: wss://username:password@brd.superproxy.io:9222
    return f"wss://{username}:{password}@brd.superproxy.io:9222"

# ============================================================================
# DIRECT CSS SELECTORS (FALLBACK STRATEGY)
# ============================================================================

FIELD_SELECTORS = {
    "full_name": [
        "input[name='name']",
        "input[name='fullName']",
        "input[name='full_name']",
        "input[aria-label*='name' i]",
        "input[placeholder*='full name' i]",
        "input[placeholder*='your name' i]",
        "input[id*='name' i]"
    ],
    "first_name": [
        "input[name='firstName']",
        "input[name='first_name']",
        "input[name='first-name']",
        "input[aria-label*='first' i]",
        "input[placeholder*='first' i]",
        "input[id*='first' i]"
    ],
    "last_name": [
        "input[name='lastName']",
        "input[name='last_name']",
        "input[name='last-name']",
        "input[aria-label*='last' i]",
        "input[placeholder*='last' i]",
        "input[id*='last' i]"
    ],
    "email": [
        "input[type='email']",
        "input[name='email']",
        "input[aria-label*='email' i]",
        "input[placeholder*='email' i]",
        "input[id*='email' i]"
    ],
    "phone": [
        "input[type='tel']",
        "input[name='phone']",
        "input[name='mobile']",
        "input[aria-label*='phone' i]",
        "input[placeholder*='phone' i]",
        "input[id*='phone' i]"
    ],
    "location": [
        "input[name='location']",
        "input[name='city']",
        "input[aria-label*='location' i]",
        "input[placeholder*='location' i]",
        "input[placeholder*='city' i]"
    ],
    "company": [
        "input[name='company']",
        "input[name='current_company']",
        "input[aria-label*='company' i]",
        "input[placeholder*='company' i]"
    ],
    "linkedin": [
        "input[name='linkedin']",
        "input[name='linkedin_url']",
        "input[aria-label*='linkedin' i]",
        "input[placeholder*='linkedin' i]"
    ]
}

async def analyze_screenshot_with_vision(
    screenshot_b64: str, 
    logs: List[str], 
    openai_key: Optional[str] = None, 
    cv_text: Optional[str] = None, 
    user_data: Optional[Dict[str, str]] = None
) -> Dict:
    """
    Analisa screenshot com GPT Vision para verificar:
    - success: True/False (se candidatura foi bem-sucedida)
    - reason: explicação
    - instructions: lista de ações para corrigir (se não foi sucesso)
    - captcha_type: tipo de CAPTCHA (se detectado)
    """
    if not openai_key:
        logger.warning("⚠ OPENAI_API_KEY não fornecida - pulando Vision")
        logs.append("⚠ Vision AI não disponível (API key em falta)")
        return {"success": False, "reason": "API key not provided", "instructions": []}
    
    try:
        logger.info("🔍 Analisando screenshot com GPT-4 Vision...")
        logs.append("🔍 Analisando página com Vision AI...")
        
        # Compactar CV text
        cv_excerpt = None
        if cv_text:
            cv_excerpt = cv_text.strip()[:4000]
        
        known_fields = {k: v for k, v in (user_data or {}).items() if k in [
            "full_name","email","phone","location","current_company","linkedin_url","years_of_experience"
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
                            "content": """You are a Playwright automation expert analyzing job application screenshots. Return STRICT JSON (no markdown).

FORMAT:
{
  "success": true/false,
  "reason": "explanation",
  "instructions": [
    {
      "action": "fill",
      "css_selector": "input[name='email']",
      "field_label": "Email Address",
      "value": "candidate@example.com"
    }
  ],
  "captcha_type": "iframe" (if present)
}

CRITICAL RULES FOR CSS SELECTORS:
1. Look at EVERY input field in the screenshot
2. For EACH empty or incorrect field, extract the EXACT CSS selector from HTML attributes:
   - Priority 1: input[name='exact-name-attribute']
   - Priority 2: input[id='exact-id-attribute']
   - Priority 3: input[type='email'] or input[type='tel']
   - Priority 4: input[aria-label='exact-aria-label']
   - Priority 5: input[placeholder='exact-placeholder']

3. Actions: "fill" (text inputs), "select" (dropdowns), "check" (checkboxes), "click" (buttons)
4. For "field_label", use the EXACT visible label text
5. For "value", use data from CV or known_fields

EXAMPLE:
If you see an input with name="applicant_email" that's empty:
{"action": "fill", "css_selector": "input[name='applicant_email']", "field_label": "Email", "value": "from_cv"}"""
                        },
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": (
                                    "Analyze this job application form screenshot. "
                                    "1. Check if submission was successful (look for success messages, confirmation pages)\n"
                                    "2. If NOT successful, find ALL empty or incorrect input fields\n"
                                    "3. For each field, extract the EXACT CSS selector from HTML attributes visible in the screenshot\n"
                                    "4. Provide Playwright instructions with specific CSS selectors\n\n"
                                    "Known candidate data: " + str(known_fields) + "\n\n"
                                    "CV excerpt:\n" + (cv_excerpt or "")
                                )},
                                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}}
                            ]
                        }
                    ]
                }
            )
            
            if response.status_code != 200:
                error_text = response.text
                logger.error(f"Vision API error: {response.status_code} - {error_text[:200]}")
                logs.append(f"❌ Vision API error: {response.status_code}")
                return {"success": False, "reason": "API error", "instructions": []}
            
            data = response.json()
            logger.info("📥 Vision API response OK")
            
            if "choices" not in data or not data["choices"]:
                logger.error(f"Invalid Vision response: {str(data)[:200]}")
                logs.append("❌ Resposta inválida do Vision AI")
                return {"success": False, "reason": "Invalid API response", "instructions": []}
            
            content = data["choices"][0]["message"]["content"]
            logger.debug(f"Vision content: {content[:100]}...")
            
            # Limpar markdown
            content_clean = content.strip()
            if content_clean.startswith("```json"):
                content_clean = content_clean[7:]
            if content_clean.startswith("```"):
                content_clean = content_clean[3:]
            if content_clean.endswith("```"):
                content_clean = content_clean[:-3]
            content_clean = content_clean.strip()
            
            # Parse JSON
            try:
                result = json.loads(content_clean)
            except json.JSONDecodeError as e:
                logger.error(f"JSON decode error: {e}, trying regex fallback...")
                json_match = re.search(r'\{[\s\S]*\}', content_clean)
                if json_match:
                    result = json.loads(json_match.group(0))
                else:
                    logger.error("Failed to extract JSON from Vision response")
                    logs.append("❌ Não foi possível interpretar resposta do Vision AI")
                    return {"success": False, "reason": "Failed to parse", "instructions": []}
            
            if result.get("success"):
                logger.info(f"✅ Vision confirmou sucesso: {result.get('reason', '')}")
                logs.append(f"✅ Vision AI: {result.get('reason', 'Candidatura bem-sucedida')}")
            else:
                logger.warning(f"⚠️ Vision detectou problemas: {result.get('reason', '')}")
                logs.append(f"⚠️ Vision AI: {result.get('reason', 'Campos em falta ou erros')}")
                instructions = result.get("instructions", [])
                if instructions:
                    logger.info(f"📋 {len(instructions)} instruções recebidas")
                    logs.append(f"📋 {len(instructions)} correções sugeridas")
            
            return result
            
    except Exception as e:
        logger.error(f"Vision analysis error: {e}")
        logger.error(traceback.format_exc())
        logs.append(f"❌ Erro Vision AI: {str(e)}")
        return {"success": False, "reason": str(e), "instructions": []}


async def execute_vision_instructions(page: Page, instructions: List[Dict], logs: List[str]) -> int:
    """
    Executa instruções Playwright diretas do Vision AI com CSS selectors específicos.
    Retorna número de instruções executadas com sucesso.
    """
    if not instructions:
        return 0
    
    logger.info(f"🔧 Executando {len(instructions)} instruções Playwright do Vision...")
    logs.append(f"🔧 Aplicando {len(instructions)} correções com CSS selectors...")
    executed = 0
    
    for idx, inst in enumerate(instructions, 1):
        try:
            action = inst.get("action", "fill")
            css_selector = inst.get("css_selector", "")
            field_label = inst.get("field_label", "unknown")
            value = inst.get("value", "")
            
            if not css_selector or not value:
                logger.warning(f"  ⚠️ {idx}. Instrução inválida: falta css_selector ou value")
                logs.append(f"⚠️ Instrução #{idx} inválida (falta selector/value)")
                continue
            
            logger.info(f"  📝 {idx}/{len(instructions)}: {action} '{field_label}' usando {css_selector}")
            
            try:
                # Executar comando Playwright DIRETO com o CSS selector fornecido pelo Vision AI
                loc = page.locator(css_selector).first
                
                # Esperar que o elemento esteja disponível
                await loc.wait_for(state="attached", timeout=3000)
                
                if await loc.is_visible():
                    if action == "fill":
                        await loc.click()
                        await asyncio.sleep(0.1)
                        await loc.clear()
                        await asyncio.sleep(0.1)
                        await loc.fill(value)
                        await asyncio.sleep(0.2)
                        logs.append(f"✅ Preenchido '{field_label}' = '{value}'")
                        logger.info(f"    ✅ SUCCESS: filled '{field_label}'")
                        executed += 1
                        
                    elif action == "select":
                        await loc.select_option(label=value)
                        logs.append(f"✅ Selecionado '{value}' em '{field_label}'")
                        logger.info(f"    ✅ SUCCESS: selected '{value}'")
                        executed += 1
                        
                    elif action == "check":
                        if not await loc.is_checked():
                            await loc.check()
                        logs.append(f"✅ Marcado '{field_label}'")
                        logger.info(f"    ✅ SUCCESS: checked '{field_label}'")
                        executed += 1
                        
                    elif action == "click":
                        await loc.click()
                        logs.append(f"✅ Clicado '{field_label}'")
                        logger.info(f"    ✅ SUCCESS: clicked '{field_label}'")
                        executed += 1
                else:
                    logger.warning(f"    ⚠️ Elemento não visível: {css_selector}")
                    logs.append(f"⚠️ #{idx} Elemento não visível: {field_label}")
                    
            except TimeoutError:
                logger.error(f"    ❌ Timeout esperando por: {css_selector}")
                logs.append(f"❌ #{idx} Timeout: {field_label}")
            except Exception as e:
                logger.error(f"    ❌ Erro ao executar: {str(e)[:100]}")
                logs.append(f"❌ #{idx} Erro: {field_label} - {str(e)[:50]}")
                
        except Exception as e:
            logger.error(f"❌ Erro executando instrução {idx}: {e}")
            logger.error(traceback.format_exc())
    
    logger.info(f"✅ Executadas {executed}/{len(instructions)} instruções Playwright")
    logs.append(f"✅ Aplicadas {executed}/{len(instructions)} correções Vision AI")
    return executed


async def detect_captcha(page: Page) -> Tuple[bool, str]:
    """Detect CAPTCHA on page."""
    try:
        # Check for common CAPTCHA indicators
        captcha_selectors = [
            'iframe[title*="captcha" i]',
            'iframe[title*="recaptcha" i]',
            '.g-recaptcha',
            '.h-captcha',
            '#captcha',
        ]
        
        for selector in captcha_selectors:
            if await page.locator(selector).count() > 0:
                logger.info(f"🔒 CAPTCHA detected: {selector}")
                return True, selector
        
        return False, ""
    except Exception as e:
        logger.error(f"Error detecting CAPTCHA: {e}")
        return False, ""

async def wait_for_captcha_resolution(
    page: Page, 
    max_wait_seconds: int = 90,
    logs: Optional[List[str]] = None
) -> Tuple[bool, float]:
    """
    Wait for Bright Data to automatically resolve CAPTCHA.
    
    Periodically checks if CAPTCHA has disappeared from the page.
    Returns (captcha_resolved, wait_time_seconds)
    """
    start_time = time.time()
    check_interval = 5  # Check every 5 seconds
    
    if logs:
        logs.append(f"⏳ Waiting for Bright Data to resolve CAPTCHA (max {max_wait_seconds}s)...")
    logger.info(f"⏳ Starting CAPTCHA resolution wait (max {max_wait_seconds}s)")
    
    while (time.time() - start_time) < max_wait_seconds:
        try:
            # Check if CAPTCHA is still present
            captcha_present, captcha_type = await detect_captcha(page)
            
            if not captcha_present:
                elapsed = time.time() - start_time
                if logs:
                    logs.append(f"✅ CAPTCHA resolved by Bright Data in {elapsed:.1f}s!")
                logger.info(f"✅ CAPTCHA resolved successfully in {elapsed:.1f}s")
                return True, elapsed
            
            # Log progress
            elapsed = time.time() - start_time
            if logs and int(elapsed) % 15 == 0:  # Log every 15 seconds
                logs.append(f"⏰ Still waiting... {elapsed:.0f}s / {max_wait_seconds}s")
            logger.debug(f"⏰ CAPTCHA still present after {elapsed:.1f}s")
            
            # Wait before next check
            await asyncio.sleep(check_interval)
            
        except Exception as e:
            logger.error(f"Error checking CAPTCHA status: {e}")
            await asyncio.sleep(check_interval)
    
    # Timeout reached
    elapsed = time.time() - start_time
    if logs:
        logs.append(f"⚠️ CAPTCHA still present after {elapsed:.1f}s")
    logger.warning(f"⚠️ CAPTCHA resolution timeout after {elapsed:.1f}s")
    
    return False, elapsed

# ============================================================================
# PLATFORM ADAPTERS
# ============================================================================

async def detect_platform(page: Page, url: str) -> str:
    """Detect job application platform."""
    try:
        domain = urlparse(url).netloc.lower()
        html = await page.content()
        html_lower = html.lower()
        
        if "greenhouse" in domain or "greenhouse" in html_lower:
            return "greenhouse"
        elif "lever" in domain or "lever.co" in domain:
            return "lever"
        elif "ashby" in domain or "ashbyhq.com" in domain:
            return "ashby"
        elif "workday" in domain:
            return "workday"
        elif "jobvite" in domain:
            return "jobvite"
        else:
            return "unknown"
    except Exception as e:
        logger.error(f"Error detecting platform: {e}")
        return "unknown"

async def greenhouse_adapter(page: Page, request: AutoApplyRequest, logs: List[str]) -> Dict[str, Any]:
    """Greenhouse-specific logic with robust name filling."""
    logger.info("🌱 Using Greenhouse adapter")
    filled = []
    
    try:
        # Name: try full_name first, then split if needed
        if request.full_name:
            # Phase 1: Try full name field
            logger.info("📝 Trying full_name field first...")
            if await fill_field_with_css_fallback(page, "full_name", request.full_name, logs):
                filled.append("full_name")
            else:
                # Phase 2: Split and try first_name + last_name
                logger.info("📝 full_name failed, splitting into first+last...")
                name_parts = request.full_name.split(" ", 1)
                first_name = name_parts[0] if len(name_parts) > 0 else ""
                last_name = name_parts[1] if len(name_parts) > 1 else ""
                
                if first_name and await fill_field_with_css_fallback(page, "first_name", first_name, logs):
                    filled.append("first_name")
                if last_name and await fill_field_with_css_fallback(page, "last_name", last_name, logs):
                    filled.append("last_name")
        
        if request.email and await fill_field_with_css_fallback(page, "email", request.email, logs):
            filled.append("email")
        
        if request.phone and await fill_field_with_css_fallback(page, "phone", request.phone, logs):
            filled.append("phone")
        
        if request.location and await fill_field_with_css_fallback(page, "location", request.location, logs):
            filled.append("location")
        
        if request.current_company and await fill_field_with_css_fallback(page, "company", request.current_company, logs):
            filled.append("company")
        
        if request.linkedin_url and await fill_field_with_css_fallback(page, "linkedin", request.linkedin_url, logs):
            filled.append("linkedin")
        
        # Resume upload
        if request.resume:
            try:
                resume_input = page.locator('input[type="file"][name*="resume"]')
                if await resume_input.count() > 0:
                    if request.resume.startswith("data:") or "base64" in request.resume:
                        logs.append("⚠️ Base64 resume upload needs file handling")
                    else:
                        logs.append("📎 Resume upload available")
                    filled.append("resume")
            except Exception as e:
                logs.append(f"⚠️ Resume upload error: {str(e)}")
        
        return {"status": "success", "filled_fields": filled, "errors": []}
    
    except Exception as e:
        logger.error(f"Greenhouse adapter error: {e}")
        return {"status": "error", "filled_fields": filled, "errors": [str(e)]}

async def lever_adapter(page: Page, request: AutoApplyRequest, logs: List[str]) -> Dict[str, Any]:
    """Lever-specific logic: OPEN the application modal first, then fill."""
    logger.info("⚡ Using Lever adapter")
    filled = []
    
    try:
        # CRITICAL: Lever requires clicking "Apply" button to open the form modal
        logs.append("🔍 Looking for Apply button to open form...")
        
        # Try multiple selectors for the Apply button
        apply_selectors = [
            "a.postings-btn",  # Common Lever Apply button
            "a[href*='apply']",
            "button:has-text('Apply')",
            "a:has-text('Apply')",
            "button:has-text('apply')",
            "a:has-text('apply')",
        ]
        
        apply_clicked = False
        for selector in apply_selectors:
            try:
                btn = page.locator(selector).first
                if await btn.count() > 0 and await btn.is_visible():
                    logs.append(f"✅ Found Apply button: {selector}")
                    await btn.click()
                    logs.append("✅ Clicked Apply button")
                    apply_clicked = True
                    
                    # Wait for form/modal to appear
                    await asyncio.sleep(2)
                    logs.append("⏳ Waiting for form modal to load...")
                    break
            except Exception as e:
                continue
        
        if not apply_clicked:
            logs.append("⚠️ Apply button not found - assuming form is already visible")
        
        # NOW fill the form fields
        # Name: try full_name first, then split if needed
        if request.full_name:
            logger.info("📝 Trying full_name field...")
            if await fill_field_with_css_fallback(page, "full_name", request.full_name, logs):
                filled.append("full_name")
            else:
                # Fallback: split into first+last
                logger.info("📝 full_name failed, splitting...")
                name_parts = request.full_name.split(" ", 1)
                first_name = name_parts[0] if len(name_parts) > 0 else ""
                last_name = name_parts[1] if len(name_parts) > 1 else ""
                
                if first_name and await fill_field_with_css_fallback(page, "first_name", first_name, logs):
                    filled.append("first_name")
                if last_name and await fill_field_with_css_fallback(page, "last_name", last_name, logs):
                    filled.append("last_name")
        
        if request.email and await fill_field_with_css_fallback(page, "email", request.email, logs):
            filled.append("email")
        
        if request.phone and await fill_field_with_css_fallback(page, "phone", request.phone, logs):
            filled.append("phone")
        
        if request.location and await fill_field_with_css_fallback(page, "location", request.location, logs):
            filled.append("location")
        
        if request.linkedin_url and await fill_field_with_css_fallback(page, "linkedin", request.linkedin_url, logs):
            filled.append("linkedin")
        
        return {"status": "success", "filled_fields": filled, "errors": []}
    
    except Exception as e:
        logger.error(f"Lever adapter error: {e}")
        return {"status": "error", "filled_fields": filled, "errors": [str(e)]}

async def generic_adapter(page: Page, request: AutoApplyRequest, logs: List[str]) -> Dict[str, Any]:
    """Generic fallback adapter with robust filling."""
    logger.info("🔧 Using generic adapter")
    filled = []
    
    try:
        # Name: try full_name first, then split if needed
        if request.full_name:
            logger.info("📝 Trying full_name field...")
            if await fill_field_with_css_fallback(page, "full_name", request.full_name, logs):
                filled.append("full_name")
            else:
                # Try alternative label "name"
                if await fill_field_robust(page, "name", request.full_name, logs):
                    filled.append("name")
                else:
                    # Fallback: split
                    logger.info("📝 Splitting name into first+last...")
                    name_parts = request.full_name.split(" ", 1)
                    first_name = name_parts[0] if len(name_parts) > 0 else ""
                    last_name = name_parts[1] if len(name_parts) > 1 else ""
                    
                    if first_name and await fill_field_with_css_fallback(page, "first_name", first_name, logs):
                        filled.append("first_name")
                    if last_name and await fill_field_with_css_fallback(page, "last_name", last_name, logs):
                        filled.append("last_name")
        
        # Other fields
        if request.email and await fill_field_with_css_fallback(page, "email", request.email, logs):
            filled.append("email")
        
        if request.phone and await fill_field_with_css_fallback(page, "phone", request.phone, logs):
            filled.append("phone")
        
        if request.location and await fill_field_with_css_fallback(page, "location", request.location, logs):
            filled.append("location")
        
        if request.current_company and await fill_field_with_css_fallback(page, "company", request.current_company, logs):
            filled.append("company")
        
        if request.linkedin_url and await fill_field_with_css_fallback(page, "linkedin", request.linkedin_url, logs):
            filled.append("linkedin")
        
        return {"status": "success", "filled_fields": filled, "errors": []}
    
    except Exception as e:
        logger.error(f"Generic adapter error: {e}")
        return {"status": "error", "filled_fields": filled, "errors": [str(e)]}

# ============================================================================
# VISION AI ANALYSIS
# ============================================================================

async def analyze_form_with_vision(screenshot_b64: str, openai_key: str) -> Dict[str, Any]:
    """
    Use GPT-4 Vision to analyze the form screenshot.
    Returns structured data about fields, CAPTCHA, and submit button.
    """
    logger.info("🔍 Analyzing form with Vision AI...")
    
    if not openai_key:
        logger.warning("⚠️ No OpenAI API key provided, skipping Vision analysis")
        return {
            "fields": [],
            "captcha": {"present": False, "type": None},
            "submit_button": {"found": False},
            "multi_page": False
        }
    
    try:
        prompt = """Analyze this job application form screenshot. For each input field visible, identify:

1. Field type (text, email, phone, textarea, file, select, checkbox, radio)
2. Label or placeholder text
3. Whether it's required (look for asterisks, "required", red indicators)
4. Approximate location (top/middle/bottom, left/center/right)
5. Suggested keywords to find this field programmatically

Also identify:
- CAPTCHA presence and type (reCAPTCHA, hCaptcha, text-based, etc.)
- Submit button text and location
- If this appears to be a multi-page form (next/continue buttons)
- Any visible error messages

Return ONLY valid JSON in this exact format:
{
  "fields": [
    {
      "type": "email",
      "label": "Email Address",
      "required": true,
      "location": "top-left",
      "selector_hints": ["email", "e-mail", "your email"]
    }
  ],
  "captcha": {
    "present": false,
    "type": null
  },
  "submit_button": {
    "found": true,
    "text": "Submit Application",
    "location": "bottom-right"
  },
  "multi_page": false,
  "errors_visible": []
}"""

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {openai_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "gpt-4o",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/png;base64,{screenshot_b64}"
                                    }
                                }
                            ]
                        }
                    ],
                    "max_tokens": 2000,
                    "temperature": 0.1
                }
            )
            
            if response.status_code != 200:
                logger.error(f"Vision API error: {response.status_code} - {response.text}")
                return {"fields": [], "captcha": {"present": False}, "submit_button": {"found": False}}
            
            result = response.json()
            content = result["choices"][0]["message"]["content"]
            
            logger.info(f"📝 Raw Vision response (first 500 chars): {content[:500]}")
            
            # Parse JSON from response
            # Remove markdown code blocks if present
            content = content.strip()
            if content.startswith("```json"):
                content = content[7:]
            if content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()
            
            if not content:
                logger.error("❌ Empty content from Vision API")
                return {"fields": [], "captcha": {"present": False}, "submit_button": {"found": False}}
            
            logger.info(f"📝 Cleaned content (first 300 chars): {content[:300]}")
            
            try:
                analysis = json.loads(content)
            except json.JSONDecodeError as je:
                logger.error(f"❌ JSON parse error: {je}")
                logger.error(f"Content that failed to parse: {content[:1000]}")
                return {"fields": [], "captcha": {"present": False}, "submit_button": {"found": False}}
            
            logger.info(f"✅ Vision AI found {len(analysis.get('fields', []))} fields")
            
            return analysis
            
    except Exception as e:
        logger.error(f"❌ Vision analysis error: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return {
            "fields": [],
            "captcha": {"present": False},
            "submit_button": {"found": False}
        }

async def validate_form_with_vision(screenshot_b64: str, openai_key: str) -> Dict[str, Any]:
    """
    Use Vision AI to validate if form is properly filled before submit.
    """
    logger.info("✅ Validating form with Vision AI...")
    
    if not openai_key:
        return {"all_filled": True, "errors": [], "warnings": []}
    
    try:
        prompt = """Analyze this job application form. Check:

1. Are all required fields filled? (look for empty fields with asterisks or "required")
2. Are there any visible error messages? (red text, error icons)
3. Are there any warnings or validation issues?
4. Does everything look ready to submit?

Return ONLY valid JSON:
{
  "all_filled": true,
  "missing_required": [],
  "errors": [],
  "warnings": [],
  "ready_to_submit": true
}"""

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {openai_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "gpt-4o",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {
                                    "type": "image_url",
                                    "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}
                                }
                            ]
                        }
                    ],
                    "max_tokens": 1000,
                    "temperature": 0.1
                }
            )
            
            if response.status_code != 200:
                return {"all_filled": True, "errors": [], "warnings": []}
            
            result = response.json()
            content = result["choices"][0]["message"]["content"].strip()
            
            # Clean markdown
            if content.startswith("```json"):
                content = content[7:]
            if content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
            
            validation = json.loads(content.strip())
            logger.info(f"✅ Validation: ready={validation.get('ready_to_submit', False)}")
            
            return validation
            
    except Exception as e:
        logger.error(f"Validation error: {e}")
        return {"all_filled": True, "errors": [], "warnings": []}

async def verify_submission_with_vision(screenshot_b64: str, openai_key: str) -> Tuple[bool, str]:
    """
    Use Vision AI to verify if submission was successful.
    """
    logger.info("🔍 Verifying submission with Vision AI...")
    
    if not openai_key:
        # Fallback to original verification
        return False, "No OpenAI key for Vision verification"
    
    try:
        prompt = """Analyze this screenshot after form submission. Determine:

1. Did the submission succeed? (look for success messages, confirmation pages, thank you messages)
2. Are we on a new page or still on the form?
3. Are there any error messages visible?

Return ONLY valid JSON:
{
  "success": true,
  "confidence": "high",
  "indicators": ["thank you message visible", "confirmation page"],
  "errors": []
}

Confidence levels: high, medium, low"""

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {openai_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "gpt-4o",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {
                                    "type": "image_url",
                                    "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}
                                }
                            ]
                        }
                    ],
                    "max_tokens": 500,
                    "temperature": 0.1
                }
            )
            
            if response.status_code != 200:
                return False, "Vision API error"
            
            result = response.json()
            content = result["choices"][0]["message"]["content"].strip()
            
            # Clean markdown
            if content.startswith("```json"):
                content = content[7:]
            if content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
            
            verification = json.loads(content.strip())
            success = verification.get("success", False)
            indicators = ", ".join(verification.get("indicators", []))
            
            message = f"Vision verification: {verification.get('confidence', 'unknown')} confidence - {indicators}"
            logger.info(f"{'✅' if success else '❌'} {message}")
            
            return success, message
            
    except Exception as e:
        logger.error(f"Vision verification error: {e}")
        return False, f"Vision verification failed: {str(e)}"

async def verify_submission(page: Page) -> Tuple[bool, str]:
    """Verify if submission was successful (2 of 3 checks)."""
    checks_passed = 0
    details = []
    
    try:
        url = page.url.lower()
        
        # Check 1: URL contains success keywords
        if any(keyword in url for keyword in ["thank", "confirm", "applied", "success"]):
            checks_passed += 1
            details.append("✅ URL indicates success")
        
        # Check 2: Success message visible
        success_texts = ["thank you", "application submitted", "we received", "application received"]
        page_text = (await page.content()).lower()
        if any(text in page_text for text in success_texts):
            checks_passed += 1
            details.append("✅ Success message found")
        
        # Check 3: Submit button gone/disabled
        try:
            submit_buttons = page.locator('button[type="submit"], input[type="submit"]')
            if await submit_buttons.count() == 0:
                checks_passed += 1
                details.append("✅ Submit button disappeared")
        except:
            pass
        
        success = checks_passed >= 2
        message = f"Verification: {checks_passed}/3 checks passed. " + " ".join(details)
        
        return success, message
    
    except Exception as e:
        logger.error(f"Verification error: {e}")
        return False, f"Verification failed: {str(e)}"

# ============================================================================
# SCREENSHOTS
# ============================================================================

async def take_screenshot(page: Page, name: str) -> str:
    """Take screenshot and return base64."""
    try:
        screenshot_bytes = await page.screenshot(full_page=False)
        b64 = base64.b64encode(screenshot_bytes).decode('utf-8')
        return f"data:image/png;base64,{b64}"
    except Exception as e:
        logger.error(f"Screenshot error: {e}")
        return ""

# ============================================================================
# MAIN AUTO-APPLY LOGIC
# ============================================================================

async def auto_apply_job(request: AutoApplyRequest) -> AutoApplyResponse:
    """Main auto-apply orchestration with Vision-First architecture."""
    
    run_id = hashlib.md5(f"{request.candidate_id}{request.job_url}{time.time()}".encode()).hexdigest()[:12]
    logs = []
    errors = []
    filled_fields = []
    telemetry = {
        "bd_browser_api_connected": False,
        "captcha_detected_dom": False,
        "captcha_detected_vision": False,
        "captcha_wait_initiated": False,
        "captcha_wait_time_seconds": 0.0,
        "captcha_resolved_by_brightdata": False,
        "captcha_resolution_failed": False,
        "platform": "unknown",
        "vision_analysis_used": False,
        "fields_detected_by_vision": 0,
        "start_time": datetime.utcnow().isoformat(),
    }
    
    screenshot_pre = ""
    screenshot_post = ""
    
    # Get OpenAI key from request or env
    openai_key = request.openai_api_key or os.getenv("OPENAI_API_KEY", "")
    
    logger.info(f"🚀 Starting Vision-First auto-apply run {run_id} for {request.job_url}")
    logs.append(f"🚀 Run ID: {run_id}")
    logs.append(f"🎯 Job URL: {request.job_url}")
    logs.append(f"🔧 Vision AI: {'Enabled' if openai_key else 'Disabled (no API key)'}")
    logs.append(f"🔧 Use Bright Data: {request.use_bright_data}")
    logs.append(f"📋 Candidate Data:")
    logs.append(f"   - Full Name: {request.full_name or 'MISSING'}")
    logs.append(f"   - Email: {request.email or 'MISSING'}")
    logs.append(f"   - Phone: {request.phone or 'MISSING'}")
    logs.append(f"   - Location: {request.location or 'MISSING'}")
    logs.append(f"   - Current Company: {request.current_company or 'MISSING'}")
    
    browser: Optional[Browser] = None
    
    try:
        async with async_playwright() as p:
            # Bright Data Browser API setup
            if request.use_bright_data and request.brightdata_username and request.brightdata_password:
                # Use Browser API endpoint (WSS connection)
                ws_endpoint = get_bright_data_browser_api_endpoint(
                    request.brightdata_username, 
                    request.brightdata_password
                )
                
                if ws_endpoint:
                    try:
                        logs.append(f"🔌 Connecting to Bright Data Browser API...")
                        logger.info(f"Connecting to Browser API: {ws_endpoint[:50]}...")
                        
                        # Increase timeout to 60s for international connections
                        browser = await p.chromium.connect_over_cdp(ws_endpoint, timeout=60000)
                        telemetry["bd_browser_api_connected"] = True
                        logs.append("✅ Bright Data Browser API connected")
                        logger.info("✅ Bright Data Browser API connected")
                    except Exception as e:
                        logs.append(f"❌ Failed to connect to Browser API: {str(e)}")
                        logger.error(f"Browser API connection failed: {e}")
                        
                        # Fallback to local browser instead of failing completely
                        logs.append("⚠️ Falling back to local browser...")
                        browser = await p.chromium.launch(
                            headless=True,
                            args=["--no-sandbox", "--disable-setuid-sandbox"]
                        )
                        telemetry["bd_browser_api_connected"] = False
                        telemetry["fallback_to_local"] = True
            else:
                # Launch local browser without proxy
                launch_options = {
                    "headless": True,
                    "args": ["--no-sandbox", "--disable-setuid-sandbox"]
                }
                browser = await p.chromium.launch(**launch_options)
                logs.append("🌐 Using local browser (no Bright Data)")
            
            # Get or create page with stealth configurations
            contexts = browser.contexts
            if contexts:
                context = contexts[0]
                pages = context.pages
                page = pages[0] if pages else await context.new_page()
            else:
                # Create new context with stealth settings
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                    timezone_id="America/New_York"
                )
                page = await context.new_page()
            
            # Set realistic headers
            await page.set_extra_http_headers({
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1"
            })
            
            # Navigate to job page with multiple strategies and retry logic
            logs.append(f"🌐 Navigating to {request.job_url}")
            max_retries = 3
            wait_strategies = ["domcontentloaded", "networkidle", "load"]
            
            navigation_successful = False
            for attempt in range(max_retries):
                for wait_strategy in wait_strategies:
                    try:
                        logs.append(f"🔄 Attempt {attempt + 1}/{max_retries} with '{wait_strategy}' strategy...")
                        await page.goto(request.job_url, wait_until=wait_strategy, timeout=120000)
                        logs.append(f"✅ Page loaded successfully!")
                        navigation_successful = True
                        break
                    except Exception as e:
                        logs.append(f"⚠️ '{wait_strategy}' strategy failed: {str(e)[:100]}")
                        if wait_strategy == wait_strategies[-1]:  # Last strategy
                            if attempt < max_retries - 1:
                                logs.append(f"⏳ Waiting 5 seconds before retry...")
                                await asyncio.sleep(5)
                        continue
                
                if navigation_successful:
                    break
            
            if not navigation_successful:
                raise Exception(f"Failed to load page after {max_retries} attempts with all strategies")
            
            # Simulate human behavior
            await asyncio.sleep(random.uniform(2, 4))
            await page.evaluate("window.scrollTo(0, 500)")
            await asyncio.sleep(random.uniform(1, 2))
            
            # CAPTCHA CHECK: Before taking screenshot, check if CAPTCHA is present
            # If Bright Data is enabled, give it time to resolve
            if request.use_bright_data:
                logs.append("🔍 Checking for CAPTCHA presence (DOM selectors)...")
                captcha_detected_dom, captcha_selector = await detect_captcha(page)
                
                if captcha_detected_dom:
                    telemetry["captcha_detected_dom"] = True
                    telemetry["captcha_wait_initiated"] = True
                    logs.append(f"🔒 CAPTCHA detected: {captcha_selector}")
                    logs.append("🔌 Bright Data Browser API active - waiting for automatic resolution...")
                    
                    # Wait for Bright Data to resolve CAPTCHA
                    resolved, wait_time = await wait_for_captcha_resolution(page, max_wait_seconds=90, logs=logs)
                    telemetry["captcha_wait_time_seconds"] = wait_time
                    
                    if resolved:
                        telemetry["captcha_resolved_by_brightdata"] = True
                        logs.append("✅ CAPTCHA resolved successfully, continuing with application")
                    else:
                        telemetry["captcha_resolution_failed"] = True
                        logs.append("⚠️ CAPTCHA persists, but continuing to Vision analysis...")
                else:
                    logs.append("✅ No CAPTCHA detected via DOM selectors")
            
            # STEP 1: Take pre-screenshot
            screenshot_pre = await take_screenshot(page, "pre")
            logs.append("📸 Initial screenshot captured")
            
            # Extract base64 from screenshot
            screenshot_b64 = screenshot_pre.split(",")[1] if "," in screenshot_pre else screenshot_pre
            
            # DEBUG MODE: HTML dump and input list
            if request.debug_mode:
                logs.append("🐛 DEBUG MODE: Extracting page information...")
                try:
                    # List all input fields
                    all_inputs = await page.locator("input, textarea, select").all()
                    logs.append(f"🐛 Found {len(all_inputs)} input elements:")
                    for idx, inp in enumerate(all_inputs[:20], 1):  # Limit to 20
                        try:
                            tag = await inp.evaluate("el => el.tagName")
                            name = await inp.get_attribute("name") or "no-name"
                            id_attr = await inp.get_attribute("id") or "no-id"
                            type_attr = await inp.get_attribute("type") or "no-type"
                            placeholder = await inp.get_attribute("placeholder") or "no-placeholder"
                            visible = await inp.is_visible()
                            logs.append(f"   {idx}. <{tag.lower()}> name='{name}' id='{id_attr}' type='{type_attr}' placeholder='{placeholder}' visible={visible}")
                        except:
                            pass
                    
                    # HTML snippet of form (first 5000 chars)
                    html_content = await page.content()
                    logs.append(f"🐛 HTML length: {len(html_content)} chars")
                    logs.append(f"🐛 HTML snippet (first 500 chars): {html_content[:500]}")
                except Exception as e:
                    logs.append(f"🐛 DEBUG extraction error: {str(e)}")
            
            # STEP 2: Detect platform and fill fields with adapters FIRST
            platform = await detect_platform(page, request.job_url)
            telemetry["platform"] = platform
            logs.append(f"🔍 Platform detected: {platform}")
            
            # Use platform adapters to fill fields
            adapter_result = {}
            if platform == "greenhouse":
                adapter_result = await greenhouse_adapter(page, request, logs)
            elif platform == "lever":
                adapter_result = await lever_adapter(page, request, logs)
            else:
                adapter_result = await generic_adapter(page, request, logs)
            
            filled_fields = adapter_result.get("filled_fields", [])
            errors.extend(adapter_result.get("errors", []))
            
            logs.append(f"✅ Filled {len(filled_fields)} fields with adapter: {', '.join(filled_fields)}")
            
            # DEBUG MODE: Take intermediate screenshot after adapter fill
            if request.debug_mode:
                logs.append("🐛 DEBUG MODE: Taking post-adapter screenshot...")
                try:
                    intermediate_screenshot = await take_screenshot(page, "post_adapter")
                    logs.append("🐛 Post-adapter screenshot captured")
                    telemetry["screenshot_intermediate"] = intermediate_screenshot[:100] + "..."
                except Exception as e:
                    logs.append(f"🐛 DEBUG screenshot error: {str(e)}")
            
            # STEP 3: Vision AI validation (self-healing loop)
            if openai_key:
                MAX_RETRIES = 3
                retry_count = 0
                
                while retry_count < MAX_RETRIES:
                    retry_count += 1
                    logs.append(f"🔄 Vision validation attempt {retry_count}/{MAX_RETRIES}")
                    
                    # Scroll to ensure all fields are visible
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await asyncio.sleep(0.5)
                    await page.evaluate("window.scrollTo(0, 0)")
                    await asyncio.sleep(0.5)
                    
                    # Take screenshot for validation
                    validation_screenshot = await take_screenshot(page, f"validation_{retry_count}")
                    validation_b64 = validation_screenshot.split(",")[1] if "," in validation_screenshot else validation_screenshot
                    
                    # Get CV text for Vision context (if resume provided)
                    cv_text = None
                    if request.resume:
                        # Extract text from resume if available
                        # For now, we'll pass None - can be enhanced later
                        pass
                    
                    # Build user data dict for Vision context
                    user_data = {
                        "full_name": request.full_name,
                        "email": request.email,
                        "phone": request.phone,
                        "location": request.location,
                        "current_company": request.current_company,
                        "linkedin_url": request.linkedin_url,
                        "years_of_experience": request.years_of_experience,
                    }
                    
                    # Analyze with Vision AI
                    vision_result = await analyze_screenshot_with_vision(
                        validation_b64, logs, openai_key, cv_text, user_data
                    )
                    
                    telemetry[f"vision_validation_attempt_{retry_count}"] = vision_result.get("success", False)
                    
                    if vision_result.get("success"):
                        logs.append(f"✅ Vision AI confirmed form is complete!")
                        telemetry["vision_validation_success"] = True
                        break
                    else:
                        logs.append(f"⚠️ Vision detected issues: {vision_result.get('reason', 'Unknown')}")
                        
                        # Execute Vision instructions to fix issues
                        instructions = vision_result.get("instructions", [])
                        if instructions:
                            executed_count = await execute_vision_instructions(page, instructions, logs)
                            telemetry[f"vision_corrections_attempt_{retry_count}"] = executed_count
                            
                            if executed_count > 0:
                                logs.append(f"✅ Applied {executed_count} corrections")
                                await asyncio.sleep(1)
                            else:
                                logs.append("⚠️ No corrections could be applied")
                                break
                        else:
                            logs.append("⚠️ No correction instructions from Vision")
                            break
                
                if retry_count >= MAX_RETRIES and not vision_result.get("success"):
                    logs.append(f"⚠️ Max validation retries reached, proceeding anyway")
            else:
                logs.append("⚠️ No OpenAI API key - skipping Vision validation")
            
            logs.append(f"✅ Filled {len(filled_fields)} fields: {', '.join(filled_fields)}")
            
            # Plan only mode
            if request.plan_only:
                logs.append("📋 Plan-only mode - skipping submission")
                return AutoApplyResponse(
                    status="success",
                    run_id=run_id,
                    message="Plan generated successfully",
                    screenshot_pre=screenshot_pre,
                    filled_fields=filled_fields,
                    logs=logs,
                    errors=errors,
                    telemetry=telemetry
                )
            
            # STEP 4: Pre-submit validation with Vision
            if openai_key and request.allow_submit:
                logs.append("🔍 Validating form before submit...")
                pre_submit_screenshot = await take_screenshot(page, "pre_submit")
                pre_submit_b64 = pre_submit_screenshot.split(",")[1] if "," in pre_submit_screenshot else pre_submit_screenshot
                
                validation = await validate_form_with_vision(pre_submit_b64, openai_key)
                
                if not validation.get("ready_to_submit", True):
                    logs.append(f"⚠️ Validation warnings: {', '.join(validation.get('warnings', []))}")
                    if validation.get("errors"):
                        errors.extend(validation["errors"])
                        logs.append(f"❌ Validation errors: {', '.join(validation['errors'])}")
            
            # STEP 5: Submit if allowed
            if request.allow_submit:
                logs.append("📤 Attempting to submit application...")
                
                try:
                    # Look for submit button (enhanced selectors)
                    submit_selectors = [
                        'button[type="submit"]',
                        'input[type="submit"]',
                        'button:has-text("Submit")',
                        'button:has-text("Apply")',
                        'button:has-text("Send")',
                        'button:has-text("Enviar")',
                        'button:has-text("Candidatar")',
                        '[data-test*="submit"]',
                        '[data-testid*="submit"]'
                    ]
                    
                    submit_button = page.locator(', '.join(submit_selectors))
                    if await submit_button.count() > 0:
                        await submit_button.first.click()
                        logs.append("✅ Submit button clicked")
                        await asyncio.sleep(3)
                    else:
                        logs.append("⚠️ No submit button found")
                        errors.append("Submit button not found")
                except Exception as e:
                    errors.append(f"Submit error: {str(e)}")
                    logs.append(f"❌ Submit error: {str(e)}")
            
            # STEP 6: Take post-screenshot
            screenshot_post = await take_screenshot(page, "post")
            logs.append("📸 Post-screenshot captured")
            
            # STEP 7: Verify submission with Vision AI
            post_screenshot_b64 = screenshot_post.split(",")[1] if "," in screenshot_post else screenshot_post
            
            # Detect basic success with heuristics
            basic_success, basic_msg = await verify_submission(page)
            logs.append(f"🔍 Basic verification: {basic_msg}")
            
            # Use Vision AI for final confirmation
            if openai_key:
                # Get CV text and user data for context
                cv_text = None
                user_data = {
                    "full_name": request.full_name,
                    "email": request.email,
                    "phone": request.phone,
                    "location": request.location,
                    "current_company": request.current_company,
                    "linkedin_url": request.linkedin_url,
                    "years_of_experience": request.years_of_experience,
                }
                
                # Analyze with Vision
                vision_result = await analyze_screenshot_with_vision(
                    post_screenshot_b64, logs, openai_key, cv_text, user_data
                )
                
                # Success if either Vision confirms OR basic heuristics confirm
                vision_success = vision_result.get("success", False)
                verified = vision_success or basic_success
                
                if vision_success:
                    verify_msg = f"✅ Vision AI confirmed: {vision_result.get('reason', 'Application submitted')}"
                elif basic_success:
                    verify_msg = f"✅ Heuristics confirmed: {basic_msg}"
                else:
                    verify_msg = f"⚠️ Could not confirm submission: {vision_result.get('reason', 'Unknown')}"
                
                logs.append(verify_msg)
                telemetry["vision_verified"] = vision_success
                telemetry["heuristic_verified"] = basic_success
            else:
                # Fallback to basic verification only
                verified = basic_success
                verify_msg = basic_msg
                logs.append(f"⚠️ No Vision AI - using basic verification only")
            
            telemetry["end_time"] = datetime.utcnow().isoformat()
            
            if verified:
                return AutoApplyResponse(
                    status="success",
                    run_id=run_id,
                    message="Application submitted successfully",
                    screenshot_pre=screenshot_pre,
                    screenshot_post=screenshot_post,
                    filled_fields=filled_fields,
                    logs=logs,
                    errors=errors,
                    telemetry=telemetry
                )
            else:
                return AutoApplyResponse(
                    status="verify_failed",
                    run_id=run_id,
                    message="Submission verification failed",
                    screenshot_pre=screenshot_pre,
                    screenshot_post=screenshot_post,
                    filled_fields=filled_fields,
                    logs=logs,
                    errors=errors,
                    telemetry=telemetry
                )
    
    except Exception as e:
        logger.error(f"❌ Auto-apply error: {e}")
        logger.error(traceback.format_exc())
        errors.append(str(e))
        
        return AutoApplyResponse(
            status="error",
            run_id=run_id,
            message=f"Error: {str(e)}",
            screenshot_pre=screenshot_pre,
            screenshot_post=screenshot_post,
            filled_fields=filled_fields,
            logs=logs,
            errors=errors,
            telemetry=telemetry
        )
    
    finally:
        if browser:
            await browser.close()
            logger.info(f"🔒 Browser closed for run {run_id}")

# ============================================================================
# ENDPOINTS
# ============================================================================

@app.get("/")
@app.get("/health")
@app.get("/healthz")
async def health():
    return {"status": "healthy", "version": "2.0", "service": "auto-apply"}

@app.post("/auto-apply", response_model=AutoApplyResponse)
async def auto_apply_endpoint(request: AutoApplyRequest):
    """Unified auto-apply endpoint implementing ChatGPT plan."""
    logger.info(f"📨 Received auto-apply request: {request.job_url}")
    logger.info(f"👤 Candidate data received:")
    logger.info(f"   - full_name: '{request.full_name}'")
    logger.info(f"   - email: '{request.email}'")
    logger.info(f"   - phone: '{request.phone}'")
    logger.info(f"   - location: '{request.location}'")
    logger.info(f"   - current_company: '{request.current_company}'")
    logger.info(f"   - linkedin_url: '{request.linkedin_url}'")
    logger.info(f"   - years_of_experience: '{request.years_of_experience}'")
    logger.info(f"   - resume: {'YES (base64)' if request.resume else 'NO'}")
    logger.info(f"🔧 Settings:")
    logger.info(f"   - use_bright_data: {request.use_bright_data}")
    logger.info(f"   - use_mcp: {request.use_mcp}")
    logger.info(f"   - allow_submit: {request.allow_submit}")
    
    try:
        result = await auto_apply_job(request)
        return result
    except Exception as e:
        logger.error(f"❌ Endpoint error: {e}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))
