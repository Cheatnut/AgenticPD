# -*- coding: utf-8 -*-
"""agents/base.py — BaseAgent template method + shared constants."""
from __future__ import annotations
import abc
import logging
from typing import Any, Dict

import config
from config import FrameworkConfig

log = logging.getLogger("agents")

STAGE_CN: Dict[str, str] = {
    "FP": "Floorplan",
    "PL": "Standard Cell Placement",
    "CTS": "Clock Tree Synthesis",
    "RT": "Routing",
}

class AgentOutputError(Exception):
    """LLM output parsed as valid JSON but failed business-rule validation"""


class BaseAgent(abc.ABC):
    tag: str = "base"

    def __init__(self, llm: Any, cfg: FrameworkConfig):
        self.llm = llm
        self.cfg = cfg

    @abc.abstractmethod
    def system_prompt(self) -> str: ...

    @abc.abstractmethod
    def build_user_prompt(self, context: Dict[str, Any]) -> str: ...

    @abc.abstractmethod
    def schema_desc(self) -> str: ...

    @abc.abstractmethod
    def validate(self, raw: Dict[str, Any],
                 context: Dict[str, Any]) -> Dict[str, Any]: ...

    def act(self, context: Dict[str, Any]) -> Dict[str, Any]:
        system = self.system_prompt()
        user = self.build_user_prompt(context)
        log.debug("[%s] SYSTEM PROMPT:\n%s", self.tag, system)
        log.debug("[%s] USER PROMPT:\n%s", self.tag, user)
        raw = self.llm.chat_json(
            system=system, user=user,
            schema_desc=self.schema_desc(), tag=self.tag)
        return self.validate(raw, context)


# =========================================================================
# JudgeAgent (paper §4)
# =========================================================================

