"""Claude as the brain's chat model. The brain keeps its history in OpenAI chat format (it also runs on
Sarvam), so this client converts that history to the Messages API shape on every call."""
import json
import time

import anthropic
from loguru import logger

from .llm import LLMError, LLMResult, ToolCall

DEFAULT_MODEL = "claude-haiku-5-5"
CALL_STARTED = "(The call has connected.)"


def _tools(tools: list[dict]) -> list[dict]:
    return [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
             "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}}
            for t in tools]


def _arguments(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def convert(messages: list[dict]) -> tuple[str, list[dict], set[str]]:
    """OpenAI-style history -> (system, Messages API messages, tool names used in the history)."""
    system_parts, out, used = [], [], set()

    def add(role: str, blocks: list[dict]) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for m in messages:
        role = m.get("role")
        if role == "system":
            if not out:
                system_parts.append(m["content"])
            else:   # a later operator note; plain text keeps it valid on every model
                add("user", [{"type": "text", "text": f"[system note] {m['content']}"}])
        elif role == "user":
            add("user", [{"type": "text", "text": m.get("content") or "…"}])
        elif role == "assistant":
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                fn = tc["function"]
                used.add(fn["name"])
                blocks.append({"type": "tool_use", "id": tc["id"], "name": fn["name"], "input": _arguments(fn["arguments"])})
            add("assistant", blocks)
        elif role == "tool":
            add("user", [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": str(m.get("content") or "ok")}])
    if out and out[0]["role"] != "user":   # the history opens with the greeting
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": CALL_STARTED}]})
    if not out:
        out.append({"role": "user", "content": [{"type": "text", "text": CALL_STARTED}]})
    return "\n\n".join(system_parts), out, used


class ClaudeChat:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, timeout: float = 8.0):
        self.model = model
        self.client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=1)

    async def complete(self, messages: list[dict], tools: list[dict]) -> LLMResult:
        started = time.monotonic()
        system, history, used = convert(messages)
        kwargs: dict = {"model": self.model, "max_tokens": 2048, "messages": history,
                        # Short classification and one-line replies: low effort keeps voice turns fast.
                        "output_config": {"effort": "low"}}
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = _tools(tools)
            # Every turn must be a tool call (plain replies go through `say`), the same contract as on Sarvam.
            # Forced tool calls skip thinking, which also keeps latency down.
            kwargs["tool_choice"] = ({"type": "tool", "name": tools[0]["function"]["name"]} if len(tools) == 1
                                     else {"type": "any"})
        elif used:
            # The API needs tool definitions whenever the history holds tool blocks; offer none to call.
            kwargs["tools"] = [{"name": n, "description": "Not available now.",
                                "input_schema": {"type": "object", "properties": {}}} for n in sorted(used)]
            kwargs["tool_choice"] = {"type": "none"}
        try:
            resp = await self.client.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            raise LLMError(f"rate limited: {e.message}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"{e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:   # includes timeouts
            raise LLMError(f"{type(e).__name__}: {e}") from e
        if resp.stop_reason == "refusal":
            raise LLMError("model declined the request")
        text = " ".join(b.text for b in resp.content if b.type == "text").strip()
        calls = [ToolCall(id=b.id, name=b.name, arguments=b.input if isinstance(b.input, dict) else {})
                 for b in resp.content if b.type == "tool_use"]
        if not text and not calls:
            logger.info(f"Empty Claude reply (stop_reason={resp.stop_reason})")
        return LLMResult(content=text or None, tool_calls=calls, ms=int((time.monotonic() - started) * 1000))
