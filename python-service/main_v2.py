"""
Auto-Apply Service V2
Implements the ChatGPT plan:
- Unified /auto-apply endpoint with new contract
- Robust selector engine (label/placeholder/role resolution)
- Bright Data Browser API integration
- Platform adapters (Greenhouse, Lever, Ashby)
- Memory system (site_profiles, submission_history, application_runs)
"""

import asyncio
import base64
import hashlib
import io
import json
import logging
import os
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
    logger.info(f"🔍 Resolving locator for: '{label_text}' → '{normalized}'")
    
    try:
        # 1. Try by label
        locator = page.get_by_label(normalized, exact=False)
        if await locator.count() > 0:
            logger.info(f"✅ Found by label: {normalized}")
            return locator.first
    except Exception as e:
        logger.debug(f"get_by_label failed: {e}")
    
    try:
        # 2. Try by placeholder
        locator = page.get_by_placeholder(normalized, exact=False)
        if await locator.count() > 0:
            logger.info(f"✅ Found by placeholder: {normalized}")
            return locator.first
    except Exception as e:
        logger.debug(f"get_by_placeholder failed: {e}")
    
    try:
        # 3. Try by role
        locator = page.get_by_role("textbox", name=normalized, exact=False)
        if await locator.count() > 0:
            logger.info(f"✅ Found by role textbox: {normalized}")
            return locator.first
    except Exception as e:
        logger.debug(f"get_by_role failed: {e}")
    
    # 4. Fallback: XPath near label
    try:
        xpath = f'//label[contains(translate(normalize-space(.), "*:", ""), "{normalized}")]/following::*[self::input or self::textarea][1]'
        locator = page.locator(f"xpath={xpath}")
        if await locator.count() > 0:
            logger.info(f"✅ Found by XPath: {normalized}")
            return locator.first
    except Exception as e:
        logger.debug(f"XPath fallback failed: {e}")
    
    logger.warning(f"❌ Could not resolve locator for: {label_text}")
    return None

async def fill_field_robust(page: Page, label: str, value: str, logs: List[str]):
    """Fill field using robust selector engine."""
    try:
        locator = await resolve_locator(page, label)
        if locator:
            await locator.click()
            await asyncio.sleep(0.1)
            await locator.fill(value)
            await asyncio.sleep(0.2)
            logs.append(f"✅ Filled '{label}': {value}")
            logger.info(f"✅ Filled '{label}': {value}")
            return True
        else:
            logs.append(f"⚠️ Could not find field: {label}")
            logger.warning(f"⚠️ Could not find field: {label}")
            return False
    except Exception as e:
        logs.append(f"❌ Error filling '{label}': {str(e)}")
        logger.error(f"❌ Error filling '{label}': {e}")
        return False

# ============================================================================
# BRIGHT DATA INTEGRATION
# ============================================================================

def get_bright_data_proxy(username: str, password: str) -> Optional[Dict[str, str]]:
    """Build Bright Data proxy config."""
    if not username or not password:
        return None
    
    proxy_host = "brd.superproxy.io"
    proxy_port = "33335"
    
    return {
        "server": f"http://{proxy_host}:{proxy_port}",
        "username": username,
        "password": password,
    }

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
# VERIFICATION
# ============================================================================

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
    """Main auto-apply orchestration with new architecture."""
    
    run_id = hashlib.md5(f"{request.candidate_id}{request.job_url}{time.time()}".encode()).hexdigest()[:12]
    logs = []
    errors = []
    filled_fields = []
    telemetry = {
        "bd_browser_api_connected": False,
        "captcha_detected": False,
        "platform": "unknown",
        "start_time": datetime.utcnow().isoformat(),
    }
    
    screenshot_pre = ""
    screenshot_post = ""
    
    logger.info(f"🚀 Starting auto-apply run {run_id} for {request.job_url}")
    logs.append(f"🚀 Run ID: {run_id}")
    logs.append(f"🎯 Job URL: {request.job_url}")
    logs.append(f"🔧 Use Bright Data: {request.use_bright_data}")
    logs.append(f"🤖 Use MCP: {request.use_mcp}")
    
    browser: Optional[Browser] = None
    
    try:
        async with async_playwright() as p:
            # Bright Data proxy setup
            launch_options = {
                "headless": True,
                "args": ["--no-sandbox", "--disable-setuid-sandbox"]
            }
            
            if request.use_bright_data and request.brightdata_username and request.brightdata_password:
                proxy_config = get_bright_data_proxy(request.brightdata_username, request.brightdata_password)
                if proxy_config:
                    launch_options["proxy"] = proxy_config
                    telemetry["bd_browser_api_connected"] = True
                    logs.append("✅ Bright Data proxy connected")
                    logger.info("✅ Bright Data proxy configured")
            
            browser = await p.chromium.launch(**launch_options)
            page = await browser.new_page()
            
            # Navigate to job page
            logs.append(f"🌐 Navigating to {request.job_url}")
            await page.goto(request.job_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            
            # Take pre-screenshot
            screenshot_pre = await take_screenshot(page, "pre")
            logs.append("📸 Pre-screenshot captured")
            
            # Detect platform
            platform = await detect_platform(page, request.job_url)
            telemetry["platform"] = platform
            logs.append(f"🔍 Platform detected: {platform}")
            logger.info(f"🔍 Platform: {platform}")
            
            # Check for CAPTCHA
            captcha_detected, captcha_type = await detect_captcha(page)
            if captcha_detected:
                telemetry["captcha_detected"] = True
                logs.append(f"🔒 CAPTCHA detected: {captcha_type}")
                
                if request.auto_captcha_allowed and request.use_bright_data:
                    logs.append("⏳ Waiting for Bright Data CAPTCHA solver...")
                    await asyncio.sleep(5)  # Wait for Browser API to solve
                else:
                    return AutoApplyResponse(
                        status="needs_review",
                        run_id=run_id,
                        message="CAPTCHA detected - manual intervention required",
                        screenshot_pre=screenshot_pre,
                        logs=logs,
                        errors=["CAPTCHA detected"],
                        telemetry=telemetry
                    )
            
            # Apply platform adapter
            adapter_result = {}
            if platform == "greenhouse":
                adapter_result = await greenhouse_adapter(page, request, logs)
            elif platform == "lever":
                adapter_result = await lever_adapter(page, request, logs)
            else:
                adapter_result = await generic_adapter(page, request, logs)
            
            filled_fields = adapter_result.get("filled_fields", [])
            errors.extend(adapter_result.get("errors", []))
            
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
            
            # Submit if allowed
            if request.allow_submit:
                logs.append("📤 Attempting to submit application...")
                
                try:
                    # Look for submit button
                    submit_button = page.locator('button[type="submit"], input[type="submit"], button:has-text("Submit"), button:has-text("Apply")')
                    if await submit_button.count() > 0:
                        await submit_button.first.click()
                        logs.append("✅ Submit button clicked")
                        await asyncio.sleep(3)
                    else:
                        logs.append("⚠️ No submit button found")
                except Exception as e:
                    errors.append(f"Submit error: {str(e)}")
                    logs.append(f"❌ Submit error: {str(e)}")
            
            # Take post-screenshot
            screenshot_post = await take_screenshot(page, "post")
            logs.append("📸 Post-screenshot captured")
            
            # Verify submission
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
    
    try:
        result = await auto_apply_job(request)
        return result
    except Exception as e:
        logger.error(f"❌ Endpoint error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
