import os
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import asyncio
import google.generativeai as genai
from playwright.async_api import async_playwright
from dotenv import load_dotenv
import textwrap
import re
import logging

load_dotenv()

# Configure Gemini - Make sure to set your GEMINI_API_KEY environment variable
try:
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model = genai.GenerativeModel('gemini-2.5-pro')
except KeyError:
    print("ERROR: GEMINI_API_KEY environment variable not set.")
    exit(1)

# --- DOM Chunking + Iterative Agent Automation ---
MAX_CHARS_PER_CHUNK = 15000
MAX_STEPS = 6
NAVIGATION_KEYWORDS = ["goto(", ".press(", ".click(", "select_option("]


def chunk_text(text: str, max_len: int = MAX_CHARS_PER_CHUNK):
    return [text[i:i+max_len] for i in range(0, len(text), max_len)]

async def execute_action_block(page, code_block: str):
    # Sanitize and build async function
    lines = [l for l in code_block.splitlines() if l.strip()]
    indented = "\n".join(["    "+l for l in lines])
    src = "async def _step():\n" + indented
    scope = {"page": page, "asyncio": asyncio}
    exec(src, scope)
    await scope["_step"]()

# Retry & DOM extraction helpers
async def extract_relevant_dom(page):
    selectors = ['#nav-search-bar-form', '#search', '.s-main-slot', '#dp', '#productTitle']
    parts = []
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if await loc.count() > 0:
                html = await loc.first.inner_html()
                parts.append(f'<!-- {sel} -->\n{html}')
        except Exception:
            pass
    if not parts:
        full = await page.content()
        full = re.sub(r'<script[\s\S]*?</script>', '', full)
        full = re.sub(r'<style[\s\S]*?</style>', '', full)
        full = re.sub(r'\s+', ' ', full)
        return full[:60000]
    joined = '\n'.join(parts)
    return joined[:60000]

async def call_model(prompt: str):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: model.generate_content(prompt))

async def generate_with_retry(prompt: str, attempts: int = 3):
    delay = 5
    for i in range(attempts):
        try:
            return await call_model(prompt)
        except Exception as e:
            msg = str(e)
            if '429' in msg:
                m = re.search(r'retry_delay[^0-9]*(\d+)', msg)
                wait = int(m.group(1)) + 1 if m else delay
                await asyncio.sleep(wait)
                delay = min(delay * 2, 60)
            else:
                if i == attempts - 1:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
    raise RuntimeError('Failed after retries')

async def automate_with_dom(goal: str):
    collected_actions = []
    status = ""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=400)
        page = await browser.new_page()
        await page.goto("https://www.amazon.com")
        await page.wait_for_load_state()
        dom = await extract_relevant_dom(page)
        for step in range(1, MAX_STEPS+1):
            dom_chunks = chunk_text(dom)
            dom_context = []
            for idx, chunk in enumerate(dom_chunks, start=1):
                dom_context.append(f"### DOM CHUNK {idx}/{len(dom_chunks)}\n{chunk}")
            dom_joined = "\n\n".join(dom_context)
            history = "\n".join(collected_actions) if collected_actions else "(none)"
            prompt = f"""
You are an iterative Playwright automation assistant.
Goal: {goal}
Previously executed actions (in order):\n{history}\n
You are currently on a page whose REDUCED HTML DOM (important sections only) is provided below in chunks. Use only selectors that plausibly exist.
{dom_joined}

Return ONLY the next 1-4 Playwright awaitable Python lines operating on the existing 'page' object OR a single line 'DONE  <reason>'.
Rules:
- Do not redefine functions or import modules.
- Prefer existing selectors you've used before.
- After filling search box, press Enter or click search button.
- Only output raw code lines (no backticks, no commentary) or DONE line.
"""
            try:
                resp = await generate_with_retry(prompt)
                raw_text = response_to_text(resp).strip()
                if not raw_text:
                    status = "LLM returned empty output (finish_reason=STOP)."; break
                text = raw_text
            except Exception as e:
                await browser.close()
                return f"LLM error at step {step}: {e}", "\n".join(collected_actions)
            if text.upper().startswith("DONE"):
                status = text
                break
            if text.startswith("```"):
                text = text.strip('`').replace('python', '')
            text = textwrap.dedent(text).strip('\n')
            bad = ["import ", "__", "os.", "subprocess", "eval(", "exec("]
            if any(b in text for b in bad):
                await browser.close()
                return "Rejected unsafe generated code.", "\n".join(collected_actions)
            try:
                await execute_action_block(page, text)
                collected_actions.extend(text.splitlines())
            except Exception as e:
                status = f"Execution error after step {step}: {e}"
                break
            if any(kw in text for kw in NAVIGATION_KEYWORDS):
                try:
                    await page.wait_for_load_state()
                    await asyncio.sleep(1.0)
                except Exception:
                    pass
                dom = await extract_relevant_dom(page)
            else:
                dom = await extract_relevant_dom(page)
        await browser.close()
    combined = "\n".join(collected_actions)
    if not status:
        status = "Finished steps or reached max steps."
    return status, combined

app = FastAPI()

class ChatRequest(BaseModel):
    message: str

@app.get("/", response_class=HTMLResponse)
async def index():
    return """
    <html><body>
    <h2>Amazon LLM Automator</h2>
    <p>Enter what you want to do on Amazon.com, and the AI will try to automate it.</p>
    <p>Example: <i>Search for "RTX 4090" and sort by price high to low</i></p>
    <form id='chat-form'>
      <input type='text' id='msg' placeholder='What do you want to do on Amazon?' style="width: 400px;">
      <button type='submit'>Send</button>
    </form>
    <h3>Response:</h3>
    <pre id='response'></pre>
    <h3>Generated Code:</h3>
    <pre id='code'></pre>
    <script>
    document.getElementById('chat-form').onsubmit = async (e) => {
      e.preventDefault();
      const msg = document.getElementById('msg').value;
      document.getElementById('response').innerText = "Processing...";
      document.getElementById('code').innerText = "";
      const res = await fetch('/chat', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({message: msg})
      });
      const data = await res.json();
      document.getElementById('response').innerText = data.reply;
      if (data.code) {
        document.getElementById('code').innerText = data.code;
      }
    };
    </script>
    </body></html>
    """

# Helper to provide selector hints (lightweight instead of full DOM dump)
async def get_amazon_selector_hints() -> str:
    hints = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://www.amazon.com")
        try:
            await page.wait_for_selector('#twotabsearchtextbox', timeout=8000)
        except Exception:
            pass
        hints = {
            "search_box": "#twotabsearchtextbox",
            "search_submit": "#nav-search-submit-button",
            "first_result_link": ".s-result-item .a-link-normal[href]",
            "sort_dropdown": "select#s-result-sort-select"
        }
        await browser.close()
    return "\n".join(f"{k}: {v}" for k, v in hints.items())

@app.post("/chat")
async def chat(req: ChatRequest):
    try:
        status, code = await automate_with_dom(req.message)
        return {"reply": status, "code": code}
    except Exception as e:
        error_message = f"An error occurred: {e}"
        print(error_message)
        return JSONResponse(status_code=500, content={"reply": error_message})

logging.basicConfig(level=logging.INFO)

# Helper to robustly extract text from Gemini responses
def response_to_text(resp) -> str:
    try:
        if hasattr(resp, 'text') and resp.text:
            return resp.text
        # Fallback to candidates structure
        candidates = getattr(resp, 'candidates', None)
        if candidates:
            for cand in candidates:
                content = getattr(cand, 'content', None)
                if not content:
                    continue
                parts = getattr(content, 'parts', None)
                if parts:
                    texts = []
                    for part in parts:
                        t = getattr(part, 'text', None)
                        if t:
                            texts.append(t)
                    if texts:
                        return "\n".join(texts)
        return ""
    except Exception as e:
        logging.warning(f"Failed to parse LLM response: {e}")
        return ""
