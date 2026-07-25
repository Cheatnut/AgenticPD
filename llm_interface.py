# -*- coding: utf-8 -*-
"""
llm_interface.py — DeepSeek API（OpenAI 兼容格式）调用封装

包含：
1. LLMClient：真实 API 客户端。
   - API key 从环境变量读取（默认 DEEPSEEK_API_KEY），绝不硬编码；
   - 网络/限流错误指数退避重试（max_api_retries 次）；
   - LLM 输出无法解析为 JSON 时，把错误信息回喂给模型重问（max_json_retries 次）；
   - 全部耗尽后抛 LLMError，由上层（agents/optimizer）决定兜底策略。
2. MockLLMClient：零 token 的模拟客户端（--dry-run），按 tag 返回确定性的
   固定决策/参数，用于离线调试优化主循环与 prompt 渲染。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import config as cfg_mod
from config import FrameworkConfig
from utils import JsonParseError, extract_json

log = logging.getLogger("llm")


class LLMError(Exception):
    """LLM 调用最终失败（重试耗尽/认证错误等），上层需走兜底逻辑"""


class LLMClient:
    """DeepSeek API 客户端（openai SDK，OpenAI 兼容 base_url）"""

    def __init__(self, cfg: FrameworkConfig):
        self.cfg = cfg
        # 延迟导入 openai：mock 模式下无需安装该依赖
        import os

        import openai  # noqa: PLC0415

        self._openai = openai
        api_key = os.environ.get(cfg.llm_api_key_env)
        if not api_key:
            raise LLMError(
                f"环境变量 {cfg.llm_api_key_env} 未设置。"
                f"请在 flow/agenticpd/.env 中配置（参考 .env.example）"
                f"或 export {cfg.llm_api_key_env}=sk-... 后重试。")
        self.client = openai.OpenAI(base_url=cfg.llm_base_url, api_key=api_key)

    # ------------------------------------------------------------------
    def chat_json(self, system: str, user: str, schema_desc: str,
                  tag: str = "") -> Dict[str, Any]:
        """发起对话并要求返回 JSON 对象；解析失败自动回喂重问。

        参数:
            system:      system prompt
            user:        user prompt
            schema_desc: 期望的 JSON schema 文字描述（重问时回喂给模型）
            tag:         调用方标识（仅用于日志，如 "judge" / "stage:FP"）
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
                log.debug("[%s] LLM JSON 解析成功：%s", tag, result)
                return result
            except JsonParseError as e:
                json_attempts += 1
                log.warning("[%s] LLM 输出 JSON 解析失败（第 %d/%d 次）：%s",
                            tag, json_attempts, self.cfg.max_json_retries, e.reason)
                if json_attempts >= self.cfg.max_json_retries:
                    raise LLMError(
                        f"[{tag}] JSON 解析重试 {json_attempts} 次仍失败："
                        f"{e.reason}") from e
                # 把模型的坏输出与错误原因一起回喂，要求严格重发 JSON
                messages.append({"role": "assistant", "content": text})
                messages.append({"role": "user", "content": (
                    f"你上一条输出无法解析为 JSON（原因：{e.reason}）。"
                    f"请重新输出，只输出一个 JSON 对象，不要包含任何解释文字或"
                    f"markdown 代码块之外的内容。JSON 格式要求：\n{schema_desc}")})

    # ------------------------------------------------------------------
    def _chat_once(self, messages: List[Dict[str, str]], tag: str) -> str:
        """单轮 API 调用，网络/限流类错误指数退避重试"""
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
                content = resp.choices[0].message.content
                if content is None:
                    raise LLMError(f"[{tag}] API 返回内容为空")
                return content
            except retryable as e:
                last_err = e
                wait = 2 ** attempt  # 2s / 4s / 8s 指数退避
                log.warning("[%s] API 调用失败（第 %d/%d 次）：%s，%ds 后重试",
                            tag, attempt, self.cfg.max_api_retries, e, wait)
                time.sleep(wait)
            except openai.AuthenticationError as e:
                # 认证错误重试无意义，直接失败并提示检查 key
                raise LLMError(
                    f"[{tag}] API 认证失败，请检查 "
                    f"{self.cfg.llm_api_key_env}：{e}") from e
        raise LLMError(
            f"[{tag}] API 重试 {self.cfg.max_api_retries} 次仍失败：{last_err}"
        ) from last_err


class MockLLMClient:
    """零 token 模拟客户端（--dry-run）：与 LLMClient 同签名。

    行为（确定性，便于调试断言）：
    - tag == "judge"：按 FP→PL→CTS→RT 轮询选择目标阶段，返回固定 hint；
    - tag == "stage:<S>"：返回该阶段所有参数，取值 = 范围中点向默认值方向
      按调用次数做小幅摆动（保证多次迭代参数有变化、且始终在合法范围内）。
    """

    def __init__(self, cfg: FrameworkConfig):
        self.cfg = cfg
        self._judge_calls = 0
        self._stage_calls: Dict[str, int] = {}

    def chat_json(self, system: str, user: str, schema_desc: str,
                  tag: str = "") -> Dict[str, Any]:
        if tag == "judge":
            # 论文对照版：node-stage 一致性（选择节点就唯一决定了重跑起点）
            # 轮询 branchable 节点：ROOT → iter0_FP → iter0_PL → iter0_CTS
            _NODES = ["ROOT", "iter0_FP", "iter0_PL", "iter0_CTS"]
            branch_node = _NODES[self._judge_calls % len(_NODES)]
            # 从节点推导 branch_stage（与 _next_stage_of_node 逻辑一致）
            _NODE2STAGE = {"ROOT": "FP", "iter0_FP": "PL", "iter0_PL": "CTS", "iter0_CTS": "RT"}
            stage = _NODE2STAGE[branch_node]
            self._judge_calls += 1
            downstream = [stage] + cfg_mod.STAGES[cfg_mod.STAGES.index(stage) + 1:]
            return {
                "branch_node": branch_node,
                "branch_stage": stage,
                "hints": {
                    s: (f"[mock] 请对 {stage} 做小步探索" if s in downstream
                        else f"[mock] Bef 阶段继承")
                    for s in cfg_mod.STAGES
                },
                "reason": f"[mock] 轮询，node={branch_node} stage={stage}",
            }

        if tag.startswith("stage:"):
            stage = tag.split(":", 1)[1]
            n = self._stage_calls.get(stage, 0)
            self._stage_calls[stage] = n + 1
            params: Dict[str, Any] = {}
            for spec in cfg_mod.PARAM_SPACE.get(stage, []):
                mid = (spec.vmin + spec.vmax) / 2
                base = spec.default if spec.default is not None else mid
                # 按调用次数在 ±10% 量程内确定性摆动
                offset = ((n % 3) - 1) * 0.1 * (spec.vmax - spec.vmin)
                params[spec.name] = spec.cast(base + offset)
            return {"params": params, "reason": f"[mock] 第 {n} 次扰动"}

        raise LLMError(f"MockLLMClient 不认识的 tag：{tag!r}")
