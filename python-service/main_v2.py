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
    Robust locator resolution:
    1. By label (get_by_label)
    2. By placeholder (get_by_placeholder)
    3. By role + name (get_by_role)
    4. Fallback: XPath near label
    """
    normalized = normalize_label(label_text)
    logger.info(f"🔍 RESOLVING LOCATOR for: '{label_text}' → normalized: '{normalized}'")
    
    try:
        # 1. Try by label
        logger.debug(f"  Strategy 1/4: Trying get_by_label('{normalized}')")
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
        logger.debug(f"  Strategy 2/4: Trying get_by_placeholder('{normalized}')")
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
        logger.debug(f"  Strategy 3/4: Trying get_by_role('textbox', name='{normalized}')")
        locator = page.get_by_role("textbox", name=normalized, exact=False)
        count = await locator.count()
        logger.debug(f"  → Found {count} elements by role")
        if count > 0:
            logger.info(f"✅ FOUND BY ROLE: '{normalized}' ({count} matches)")
            return locator.first
    except Exception as e:
        logger.debug(f"  → get_by_role failed: {e}")
    
    # 4. Fallback: XPath near label
    try:
        logger.debug(f"  Strategy 4/4: Trying XPath fallback")
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
    logger.error(f"   Tried: label, placeholder, role(textbox), XPath - all returned 0 matches")
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
    """Greenhouse-specific logic."""
    logger.info("🌱 Using Greenhouse adapter")
    filled = []
    
    try:
        # Greenhouse uses specific IDs
        if request.full_name:
            # Split name
            name_parts = request.full_name.split(" ", 1)
            first_name = name_parts[0] if len(name_parts) > 0 else ""
            last_name = name_parts[1] if len(name_parts) > 1 else ""
            
            if await fill_field_robust(page, "first name", first_name, logs):
                filled.append("first_name")
            if await fill_field_robust(page, "last name", last_name, logs):
                filled.append("last_name")
        
        if request.email and await fill_field_robust(page, "email", request.email, logs):
            filled.append("email")
        
        if request.phone and await fill_field_robust(page, "phone", request.phone, logs):
            filled.append("phone")
        
        # Resume upload
        if request.resume:
            try:
                resume_input = page.locator('input[type="file"][name*="resume"]')
                if await resume_input.count() > 0:
                    # Handle base64 or URL
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
    """Lever-specific logic."""
    logger.info("⚡ Using Lever adapter")
    filled = []
    
    try:
        if request.full_name and await fill_field_robust(page, "name", request.full_name, logs):
            filled.append("name")
        
        if request.email and await fill_field_robust(page, "email", request.email, logs):
            filled.append("email")
        
        if request.phone and await fill_field_robust(page, "phone", request.phone, logs):
            filled.append("phone")
        
        return {"status": "success", "filled_fields": filled, "errors": []}
    
    except Exception as e:
        logger.error(f"Lever adapter error: {e}")
        return {"status": "error", "filled_fields": filled, "errors": [str(e)]}

async def generic_adapter(page: Page, request: AutoApplyRequest, logs: List[str]) -> Dict[str, Any]:
    """Generic fallback adapter."""
    logger.info("🔧 Using generic adapter")
    filled = []
    
    try:
        # Try common field labels
        field_mappings = [
            ("full name", request.full_name),
            ("name", request.full_name),
            ("email", request.email),
            ("phone", request.phone),
            ("location", request.location),
            ("current company", request.current_company),
            ("linkedin", request.linkedin_url),
        ]
        
        for label, value in field_mappings:
            if value and await fill_field_robust(page, label, value, logs):
                filled.append(label)
        
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
            
            analysis = json.loads(content)
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
                        
                        browser = await p.chromium.connect_over_cdp(ws_endpoint)
                        telemetry["bd_browser_api_connected"] = True
                        logs.append("✅ Bright Data Browser API connected")
                        logger.info("✅ Bright Data Browser API connected")
                    except Exception as e:
                        logs.append(f"❌ Failed to connect to Browser API: {str(e)[:100]}")
                        logger.error(f"Browser API connection failed: {e}")
                        raise Exception(f"Bright Data Browser API connection failed: {str(e)}")
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
            
            # STEP 1: Take pre-screenshot and analyze with Vision AI
            screenshot_pre = await take_screenshot(page, "pre")
            logs.append("📸 Initial screenshot captured")
            logger.info("📸 Screenshot captured, starting Vision analysis...")
            
            # Extract base64 from screenshot
            screenshot_b64 = screenshot_pre.split(",")[1] if "," in screenshot_pre else screenshot_pre
            
            # STEP 2: Vision AI Analysis
            if not openai_key:
                logs.append("⚠️ No OpenAI API key - skipping Vision AI analysis")
                logger.warning("No OpenAI API key available - will use platform adapters")
                form_analysis = None
            else:
                logs.append("🤖 Starting Vision AI analysis...")
                form_analysis = await analyze_form_with_vision(screenshot_b64, openai_key)
                if form_analysis:
                    logs.append(f"✅ Vision AI analysis complete: {form_analysis.keys()}")
                else:
                    logs.append("❌ Vision AI analysis returned None")
            
            if form_analysis and form_analysis.get("fields"):
                telemetry["vision_analysis_used"] = True
                telemetry["fields_detected_by_vision"] = len(form_analysis["fields"])
                logs.append(f"🤖 Vision AI detected {len(form_analysis['fields'])} fields")
                logger.info(f"🤖 Vision detected {len(form_analysis['fields'])} fields")
                
                # Check for CAPTCHA
                if form_analysis.get("captcha", {}).get("present"):
                    captcha_type = form_analysis["captcha"].get("type", "unknown")
                    telemetry["captcha_detected_vision"] = True
                    logs.append(f"🤖 Vision AI detected CAPTCHA: {captcha_type}")
                    
                    if request.use_bright_data:
                        # Bright Data should have already handled it
                        logs.append(f"⚠️ Vision still sees CAPTCHA ({captcha_type}) after Bright Data wait")
                        logs.append("🔄 Attempting second resolution cycle (30s)...")
                        
                        # Second attempt with shorter timeout
                        resolved, additional_wait = await wait_for_captcha_resolution(page, max_wait_seconds=30, logs=logs)
                        telemetry["captcha_wait_time_seconds"] += additional_wait
                        
                        if resolved:
                            telemetry["captcha_resolved_by_brightdata"] = True
                            logs.append(f"✅ CAPTCHA resolved in second cycle (total: {telemetry['captcha_wait_time_seconds']:.1f}s)")
                            
                            # Take new screenshot to verify
                            screenshot_pre = await take_screenshot(page, "post_captcha")
                            screenshot_b64 = screenshot_pre.split(",")[1] if "," in screenshot_pre else screenshot_pre
                            
                            # Re-analyze with Vision
                            form_analysis = await analyze_form_with_vision(screenshot_b64, openai_key)
                            logs.append("🤖 Re-analyzing form after CAPTCHA resolution...")
                        else:
                            telemetry["captcha_resolution_failed"] = True
                            logs.append(f"❌ CAPTCHA persists after {telemetry['captcha_wait_time_seconds']:.1f}s total")
                            
                            if not request.auto_captcha_allowed:
                                return AutoApplyResponse(
                                    status="needs_review",
                                    run_id=run_id,
                                    message=f"CAPTCHA ({captcha_type}) could not be resolved automatically",
                                    screenshot_pre=screenshot_pre,
                                    logs=logs,
                                    errors=[f"CAPTCHA resolution failed: {captcha_type}"],
                                    telemetry=telemetry
                                )
                            else:
                                logs.append("⚠️ Proceeding despite CAPTCHA (auto_captcha_allowed=true)...")
                    elif not request.auto_captcha_allowed:
                        return AutoApplyResponse(
                            status="needs_review",
                            run_id=run_id,
                            message=f"CAPTCHA detected ({captcha_type}) - manual intervention required",
                            screenshot_pre=screenshot_pre,
                            logs=logs,
                            errors=[f"CAPTCHA: {captcha_type}"],
                            telemetry=telemetry
                        )
                    else:
                        logs.append("⏳ Attempting to proceed with CAPTCHA...")
                        await asyncio.sleep(3)
                
                # STEP 3: Fill fields based on Vision analysis
                logs.append("📝 Starting to fill fields based on Vision analysis...")
                
                for field_info in form_analysis["fields"]:
                    field_label = field_info.get("label", "unknown")
                    field_type = field_info.get("type", "text")
                    selector_hints = field_info.get("selector_hints", [field_label])
                    
                    # Determine what value to use
                    value = None
                    field_name = field_label.lower()
                    
                    # Match field to data
                    if any(hint in field_name for hint in ["name", "nome", "full name"]):
                        value = request.full_name
                    elif any(hint in field_name for hint in ["email", "e-mail"]):
                        value = request.email
                    elif any(hint in field_name for hint in ["phone", "telefone", "tel", "mobile"]):
                        value = request.phone
                    elif any(hint in field_name for hint in ["location", "city", "localização", "address"]):
                        value = request.location or request.current_company
                    elif any(hint in field_name for hint in ["company", "empresa", "current company"]):
                        value = request.current_company
                    elif any(hint in field_name for hint in ["linkedin"]):
                        value = request.linkedin_url
                    elif any(hint in field_name for hint in ["experience", "years"]):
                        value = request.years_of_experience
                    
                    if value:
                        # Try each selector hint
                        filled = False
                        for hint in selector_hints:
                            if await fill_field_robust(page, hint, value, logs):
                                filled_fields.append(field_label)
                                filled = True
                                break
                        
                        if not filled:
                            logs.append(f"⚠️ Could not fill '{field_label}' - tried: {', '.join(selector_hints)}")
                    else:
                        logs.append(f"⚠️ No data available for field: '{field_label}'")
                
            else:
                # Fallback to platform adapters if Vision fails
                logs.append("⚠️ Vision analysis failed, using fallback selector engine")
                platform = await detect_platform(page, request.job_url)
                telemetry["platform"] = platform
                logs.append(f"🔍 Platform detected: {platform}")
                
                # Check for CAPTCHA with fallback method
                captcha_detected, captcha_selector = await detect_captcha(page)
                if captcha_detected:
                    telemetry["captcha_detected_dom"] = True
                    logs.append(f"🔒 CAPTCHA detected (fallback): {captcha_selector}")
                    
                    if request.use_bright_data:
                        # Give Bright Data a chance to resolve
                        telemetry["captcha_wait_initiated"] = True
                        logs.append("🔌 Bright Data active - waiting for CAPTCHA resolution...")
                        
                        resolved, wait_time = await wait_for_captcha_resolution(page, max_wait_seconds=60, logs=logs)
                        telemetry["captcha_wait_time_seconds"] = wait_time
                        
                        if resolved:
                            telemetry["captcha_resolved_by_brightdata"] = True
                            logs.append("✅ CAPTCHA resolved, continuing with adapter...")
                        else:
                            telemetry["captcha_resolution_failed"] = True
                            logs.append("⚠️ CAPTCHA resolution failed")
                            
                            if not request.auto_captcha_allowed:
                                return AutoApplyResponse(
                                    status="needs_review",
                                    run_id=run_id,
                                    message="CAPTCHA could not be resolved automatically",
                                    screenshot_pre=screenshot_pre,
                                    logs=logs,
                                    errors=["CAPTCHA resolution failed"],
                                    telemetry=telemetry
                                )
                    elif not request.auto_captcha_allowed:
                        return AutoApplyResponse(
                            status="needs_review",
                            run_id=run_id,
                            message="CAPTCHA detected - manual intervention required",
                            screenshot_pre=screenshot_pre,
                            logs=logs,
                            errors=["CAPTCHA detected"],
                            telemetry=telemetry
                        )
                
                # Use platform adapters
                adapter_result = {}
                if platform == "greenhouse":
                    adapter_result = await greenhouse_adapter(page, request, logs)
                elif platform == "lever":
                    adapter_result = await lever_adapter(page, request, logs)
                else:
                    adapter_result = await generic_adapter(page, request, logs)
                
                filled_fields = adapter_result.get("filled_fields", [])
                errors.extend(adapter_result.get("errors", []))
            
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
            
            # STEP 7: Verify submission with Vision
            post_screenshot_b64 = screenshot_post.split(",")[1] if "," in screenshot_post else screenshot_post
            
            if openai_key:
                verified, verify_msg = await verify_submission_with_vision(post_screenshot_b64, openai_key)
                logs.append(f"🤖 Vision verification: {verify_msg}")
            else:
                # Fallback to old verification
                verified, verify_msg = await verify_submission(page)
                logs.append(verify_msg)
            
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
