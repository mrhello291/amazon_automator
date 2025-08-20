# Amazon Automator

Lightweight prototype that lets you describe a goal (e.g. "search RTX 4090 and sort by price high to low") and an LLM (Gemini 2.5 Pro) iteratively drives a real Chromium browser on Amazon using Playwright.

## What it does
- Opens Amazon in a visible browser
- Captures reduced or full DOM each step
- Asks the model for the next 1–4 Playwright actions until DONE or step limit
- Shows you the executed action history in the web UI

## Requirements
- Python 3.9+
- Conda (optional) or virtualenv
- A valid `GEMINI_API_KEY` in a `.env` file

Example `.env`:
```
GEMINI_API_KEY=your_key_here
```

## Install
```
conda create -n amazon_automator_env python=3.9 -y
conda activate amazon_automator_env
pip install -r requirements.txt
playwright install
```

## Run
```
uvicorn main:app --reload
```
Open http://127.0.0.1:8000 and enter a goal.

## Notes
- The model may hit rate limits; it auto-retries a few times.
- Generated actions are sandboxed; unsafe patterns are filtered.
- Increase `MAX_STEPS` or adjust `MAX_CHARS_PER_CHUNK` in `main.py` if needed.

## Disclaimer
Prototype quality; expect occasional selector or quota failures.