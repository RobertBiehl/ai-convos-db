"""Playwright-based browser automation for fetching from web APIs."""

import json, asyncio
from pathlib import Path
from playwright.async_api import async_playwright, Browser, BrowserContext

CLAUDE_CODE_WEB_URL = "https://claude.ai/code"
CLAUDE_AI_URL = "https://claude.ai"
CHATGPT_URL = "https://chat.openai.com"

async def get_browser_context(browser: Browser, storage_state: Path | None = None) -> BrowserContext:
    """Create browser context, optionally loading saved auth state."""
    if storage_state and storage_state.exists():
        return await browser.new_context(storage_state=str(storage_state))
    return await browser.new_context()

async def save_auth_state(context: BrowserContext, path: Path):
    """Save browser auth state for reuse."""
    path.parent.mkdir(parents=True, exist_ok=True)
    await context.storage_state(path=str(path))

async def capture_api_responses(context: BrowserContext, url: str, api_pattern: str, timeout: int = 30000) -> list[dict]:
    """Navigate to URL and capture API responses matching pattern."""
    responses = []
    page = await context.new_page()

    async def handle_response(response):
        if api_pattern in response.url and response.status == 200:
            try:
                data = await response.json()
                responses.append({"url": response.url, "data": data})
            except: pass

    page.on("response", handle_response)
    await page.goto(url, wait_until="networkidle", timeout=timeout)
    await page.wait_for_timeout(2000)  # extra time for async API calls
    await page.close()
    return responses

async def fetch_claude_code_web_sessions(storage_state: Path | None = None, headless: bool = True) -> list[dict]:
    """Fetch Claude Code web sessions using playwright.

    Returns list of session objects with structure similar to local Claude Code sessions.
    Uses browser automation to capture API responses from claude.ai/code.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await get_browser_context(browser, storage_state)

        # Navigate to claude.ai/code and capture session list API
        responses = await capture_api_responses(context, CLAUDE_CODE_WEB_URL, "/api/", timeout=60000)

        sessions = []
        for resp in responses:
            url, data = resp["url"], resp["data"]
            # Look for session list or individual session data
            if "sessions" in url or "tasks" in url or "code" in url:
                if isinstance(data, list):
                    sessions.extend(data)
                elif isinstance(data, dict) and "sessions" in data:
                    sessions.extend(data["sessions"])
                elif isinstance(data, dict) and "id" in data:
                    sessions.append(data)

        if storage_state:
            await save_auth_state(context, storage_state)

        await browser.close()
        return sessions

async def fetch_with_login(url: str, api_pattern: str, storage_state: Path | None = None) -> list[dict]:
    """Fetch API data, prompting for login if needed (non-headless)."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await get_browser_context(browser, storage_state)
        page = await context.new_page()

        responses = []
        async def handle_response(response):
            if api_pattern in response.url and response.status == 200:
                try:
                    responses.append({"url": response.url, "data": await response.json()})
                except: pass

        page.on("response", handle_response)
        await page.goto(url)

        # Wait for user to log in if needed - check for auth
        print(f"Browser opened to {url}")
        print("If you need to log in, please do so. Press Enter when ready to continue...")
        await asyncio.get_event_loop().run_in_executor(None, input)

        # Refresh to capture API calls after login
        await page.reload(wait_until="networkidle")
        await page.wait_for_timeout(3000)

        if storage_state:
            await save_auth_state(context, storage_state)

        await browser.close()
        return responses

def fetch_claude_code_web_sync(storage_state: Path | None = None, headless: bool = True) -> list[dict]:
    """Synchronous wrapper for fetch_claude_code_web_sessions."""
    return asyncio.run(fetch_claude_code_web_sessions(storage_state, headless))

# API response schemas for testing - these document expected API structures
EXPECTED_SCHEMAS = {
    "chatgpt_conversations": {
        "type": "object",
        "required": ["items", "total"],
        "properties": {
            "items": {"type": "array"},
            "total": {"type": "integer"}
        }
    },
    "chatgpt_conversation": {
        "type": "object",
        "required": ["mapping"],
        "properties": {
            "mapping": {"type": "object"}
        }
    },
    "claude_organizations": {
        "type": "array",
        "items": {"type": "object", "required": ["uuid"]}
    },
    "claude_conversations": {
        "type": "array",
        "items": {"type": "object", "required": ["uuid"]}
    },
    "claude_conversation": {
        "type": "object",
        "required": ["uuid", "chat_messages"],
        "properties": {
            "chat_messages": {"type": "array"}
        }
    }
}

def validate_schema(data: dict | list, schema_name: str) -> tuple[bool, str]:
    """Basic schema validation for API responses."""
    schema = EXPECTED_SCHEMAS.get(schema_name)
    if not schema:
        return False, f"Unknown schema: {schema_name}"

    if schema["type"] == "array":
        if not isinstance(data, list):
            return False, f"Expected array, got {type(data).__name__}"
        if data and "items" in schema:
            item_schema = schema["items"]
            for i, item in enumerate(data[:3]):  # check first 3
                if "required" in item_schema:
                    for field in item_schema["required"]:
                        if field not in item:
                            return False, f"Item {i} missing required field: {field}"
        return True, "OK"

    if schema["type"] == "object":
        if not isinstance(data, dict):
            return False, f"Expected object, got {type(data).__name__}"
        for field in schema.get("required", []):
            if field not in data:
                return False, f"Missing required field: {field}"
        return True, "OK"

    return False, f"Unknown schema type: {schema['type']}"
