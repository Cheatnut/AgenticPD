# -*- coding: utf-8 -*-
"""
llm_interface.py — DeepSeek API (OpenAI-compatible format) client wrapper

Contains:
1. LLMClient: real API client.
   - API key read from env var (default DEEPSEEK_API_KEY), never hardcoded;
   - Exponential backoff retry for network/rate-limit errors (max_api_retries times);
   - When LLM output cannot be parsed as JSON, feed the error back to the model
     for a retry (max_json_retries times);
   - When all retries exhausted, raise LLMError; upstream (agents/optimizer)
     decides the fallback strategy.
2. MockLLMClient: zero-token mock client (--mock-llm), returns deterministic
   fixed decisions/params per tag, for offline debugging of the optimization
   main loop and prompt rendering.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import config as cfg_mod
from config import FrameworkConfig
from core.utils import JsonParseError, extract_json

log = logging.getLogger("llm")


class LLMError(Exception):
    """LLM call ultimately failed (retries exhausted / auth error etc.);
    upstream must fall back to the fail-safe path"""


class LLMClient:
    """DeepSeek API client (openai SDK, OpenAI-compatible base_url)"""

    def __init__(self, cfg: FrameworkConfig):
        self.cfg = cfg
        # Deferred import of openai: not needed in mock mode
        import os

        import openai  # noqa: PLC0415

        self._openai = openai
        api_key = os.environ.get(cfg.llm_api_key_env)
        if not api_key:
            raise LLMError(
                f"Environment variable {cfg.llm_api_key_env} is not set. "
                f"Please configure it in flow/agenticpd/.env (see .env.example) "
                f"or export {cfg.llm_api_key_env}=sk-... and retry.")
        self.client = openai.OpenAI(base_url=cfg.llm_base_url, api_key=api_key)

    # ------------------------------------------------------------------
    def chat_json(self, system: str, user: str, schema_desc: str,
                  tag: str = "") -> Dict[str, Any]:
        """Send a conversation and require a JSON object in response;
        auto-feed-back on parse failure.

        Args:
            system:      system prompt
            user:        user prompt
            schema_desc: expected JSON schema description (fed back on retry)
            tag:         caller identifier (for logging only, e.g. "judge" / "stage:FP")
        """
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        json_attempts = 0
        while True:
            text = self._chat_once(messages, tag)
            try:
                result = extract_json(text)
                log.debug("[%s] LLM JSON parsed successfully: %s", tag, result)
                return result
            except JsonParseError as e:
                json_attempts += 1
                log.warning("[%s] LLM JSON parse failed (attempt %d/%d): %s",
                            tag, json_attempts, self.cfg.max_json_retries, e.reason)
                if json_attempts >= self.cfg.max_json_retries:
                    raise LLMError(
                        f"[{tag}] JSON parse retries exhausted ({json_attempts} "
                        f"attempts): {e.reason}") from e
                # Feed the bad output and error reason back to the model,
                # demanding strict JSON re-output
                messages.append({"role": "assistant", "content": text})
                messages.append({"role": "user", "content": (
                    f"Your previous output could not be parsed as JSON "
                    f"(reason: {e.reason}). "
                    f"Please re-output, providing ONLY one JSON object, with no "
                    f"explanatory text or content outside markdown code blocks. "
                    f"Required JSON format:\n{schema_desc}")})

    # ------------------------------------------------------------------
    def _chat_once(self, messages: List[Dict[str, str]], tag: str) -> str:
        """Single API call with exponential backoff retry for network/rate-limit errors"""
        openai = self._openai
        retryable = (openai.APIConnectionError, openai.APITimeoutError,
                     openai.RateLimitError, openai.InternalServerError)
        last_err: Optional[Exception] = None
        for attempt in range(1, self.cfg.max_api_retries + 1):
            try:
                resp = self.client.chat.completions.create(
                    model=self.cfg.llm_model,
                    messages=messages,
                    temperature=self.cfg.llm_temperature,
                )
                if not resp.choices:
                    raise LLMError(f"[{tag}] API returned empty choices list")
                content = resp.choices[0].message.content
                if content is None:
                    raise LLMError(f"[{tag}] API returned empty content")
                return content
            except retryable as e:
                last_err = e
                wait = 2 ** attempt  # 2s / 4s / 8s exponential backoff
                log.warning("[%s] API call failed (attempt %d/%d): %s, retrying in %ds",
                            tag, attempt, self.cfg.max_api_retries, e, wait)
                time.sleep(wait)
            except openai.AuthenticationError as e:
                # Auth errors cannot be fixed by retrying; fail immediately
                # with a hint to check the key
                raise LLMError(
                    f"[{tag}] API authentication failed, please check "
                    f"{self.cfg.llm_api_key_env}: {e}") from e
        raise LLMError(
            f"[{tag}] API retries exhausted ({self.cfg.max_api_retries} "
            f"attempts): {last_err}"
        ) from last_err


class MockLLMClient:
    """Zero-token mock client (--mock-llm): same signature as LLMClient.

    Behavior (deterministic, for easy debug assertions):
    - tag == "judge": round-robin target stages FP→PL→CTS→RT, fixed hints;
    - tag == "stage:<S>": return all params for this stage, values = range
      midpoint shifted toward default by call count (ensuring varied params
      across iterations, always within legal range).
    """

    def __init__(self, cfg: FrameworkConfig):
        self.cfg = cfg
        self._judge_calls = 0
        self._stage_calls: Dict[str, int] = {}

    def chat_json(self, system: str, user: str, schema_desc: str,
                  tag: str = "") -> Dict[str, Any]:
        if tag == "judge":
            # Paper-aligned: node-stage consistency (choosing a node uniquely
            # determines the re-run start point)
            # Round-robin branchable nodes: ROOT → iter0_FP → iter0_PL → iter0_CTS
            _NODES = ["ROOT", "iter0_FP", "iter0_PL", "iter0_CTS"]
            branch_node = _NODES[self._judge_calls % len(_NODES)]
            # Derive branch_stage from node (consistent with _next_stage_of_node)
            _NODE2STAGE = {"ROOT": "FP", "iter0_FP": "PL", "iter0_PL": "CTS", "iter0_CTS": "RT"}
            stage = _NODE2STAGE[branch_node]
            self._judge_calls += 1
            downstream = [stage] + cfg_mod.STAGES[cfg_mod.STAGES.index(stage) + 1:]
            return {
                "branch_node": branch_node,
                "branch_stage": stage,
                "hints": {
                    s: (f"[mock] explore {stage} with small steps" if s in downstream
                        else f"[mock] Bef stage inherited")
                    for s in cfg_mod.STAGES
                },
                "reason": f"[mock] round-robin, node={branch_node} stage={stage}",
            }

        if tag.startswith("stage:"):
            stage = tag.split(":", 1)[1]
            n = self._stage_calls.get(stage, 0)
            self._stage_calls[stage] = n + 1
            params: Dict[str, Any] = {}
            for spec in cfg_mod.PARAM_SPACE.get(stage, []):
                mid = (spec.vmin + spec.vmax) / 2
                base = spec.default if spec.default is not None else mid
                # Deterministic ±10% of span wobble based on call count
                offset = ((n % 3) - 1) * 0.1 * (spec.vmax - spec.vmin)
                params[spec.name] = spec.cast(base + offset)
            return {"params": params, "reason": f"[mock] perturbation #{n}"}

        raise LLMError(f"MockLLMClient unrecognized tag: {tag!r}")
