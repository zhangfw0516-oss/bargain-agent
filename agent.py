"""LLM request parsing. Parsing never executes monitoring tasks."""

import json
import os
from pathlib import Path
from schemas import validate_task


class ParserError(RuntimeError):
    """Safe configuration, provider or response error."""


def parse_user_instruction(user_instruction: str, *, client=None, model=None) -> dict:
    """Parse and validate a request. Inject a client/model for offline testing."""
    if not isinstance(user_instruction, str) or not user_instruction.strip():
        raise ParserError("Please enter a monitoring request.")
    if len(user_instruction) > 8000:
        raise ParserError("Request exceeds 8000 characters.")
    if client is None:
        try:
            from dotenv import load_dotenv
            from openai import OpenAI
        except ImportError:
            raise ParserError("Install dependencies with pip install -r requirements.txt.") from None
        load_dotenv(Path(__file__).with_name('.env'))
        key = os.getenv("LLM_API_KEY", "").strip()
        base_url = os.getenv("LLM_API_BASE_URL", "").strip()
        model = model or os.getenv("LLM_MODEL", "").strip()
        if not key or key == "your_api_key_here" or not base_url or not model:
            raise ParserError("Set LLM_API_KEY, LLM_API_BASE_URL and LLM_MODEL in .env.")
        client = OpenAI(api_key=key, base_url=base_url, timeout=30.0, max_retries=1)
    if not model:
        raise ParserError("A model ID is required.")
    prompt = Path(__file__).with_name("prompts").joinpath("parse_instruction.md").read_text(encoding="utf-8")
    try:
        response = client.chat.completions.create(
            model=model, messages=[{"role": "system", "content": prompt},
                                   {"role": "user", "content": user_instruction}],
            response_format={"type": "json_object"}, max_tokens=2048)
        choice = response.choices[0]
        if choice.finish_reason != "stop" or not choice.message.content:
            raise ParserError("Empty or incomplete model response; please retry.")
        return validate_task(json.loads(choice.message.content), user_instruction)
    except ParserError:
        raise
    except (ValueError, TypeError, AttributeError, IndexError):
        raise ParserError("Model response failed validation; no task was created.") from None
    except Exception:
        raise ParserError("LLM request failed. Check configuration, quota and connectivity.") from None
