# -*- coding: utf-8 -*-
"""
utils.py — AgenticPD utility module

Contains:
1. Logging initialization (dual channel: console + file)
2. .env file parsing (hand-rolled implementation, avoids hard python-dotenv dependency)
3. Robust JSON extraction from LLM output (strip markdown fences, extract first/last braces)
4. Atomic history file persistence (crash-safe)

Run self-tests directly: python3 agenticpd/utils.py
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from core.qor import QoR, qor_is_better



# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(log_file: Optional[Path] = None,
                  level: int = logging.INFO) -> logging.Logger:
    """Initialize root logger: console + optional file dual-channel output.

    Repeated calls are safe (existing handlers are cleared first), making
    this convenient for test scenarios that need re-initialization.
    """
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    fmt = logging.Formatter("%(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)

    return root


# ---------------------------------------------------------------------------
# .env file parsing
# ---------------------------------------------------------------------------

def load_dotenv_file(path: Path) -> None:
    """Parse a KEY=VALUE .env file and write entries into os.environ.

    Rules: skip blank lines and # comments; never override existing env vars
    (environment takes priority over file); surrounding single/double quotes
    on values are stripped. Silently returns if file doesn't exist (key may
    be provided directly from the environment).
    """
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


# ---------------------------------------------------------------------------
# Robust JSON extraction
# ---------------------------------------------------------------------------

class JsonParseError(Exception):
    """LLM output could not be parsed as JSON; the `raw` field preserves the
    original text for feeding back to the model"""

    def __init__(self, reason: str, raw: str):
        super().__init__(reason)
        self.reason = reason
        self.raw = raw


def extract_json(text: str) -> Dict[str, Any]:
    """Robustly extract a JSON object from LLM output text.

    Processing strategy (progressive degradation):
    1. Strip ```json ... ``` or ``` ... ``` markdown code fences;
    2. Try direct json.loads;
    3. If that fails, extract substring from first '{' to last '}' and retry;
    4. If still failing, raise JsonParseError (caller feeds back to LLM).

    """
    if not isinstance(text, str) or not text.strip():
        raise JsonParseError("LLM returned empty content", raw=str(text))

    cleaned = text.strip()
    # Strip markdown code fences (```json or ```), allowing surrounding
    # conversational text before/after the fence
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL | re.IGNORECASE)
    if fence:
        cleaned = fence.group(1).strip()

    for candidate in (cleaned,):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # Extract content between first '{' and last '}' (handles text with
    # explanatory prose before/after the JSON)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(cleaned[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError as e:
            raise JsonParseError(f"JSON syntax error: {e}", raw=text)

    raise JsonParseError("No JSON object found in text", raw=text)


# ---------------------------------------------------------------------------
