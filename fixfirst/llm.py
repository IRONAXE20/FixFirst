"""Chat model clients. `SarvamChat` talks to Sarvam's OpenAI-compatible endpoint;
`ScriptedLLM` replays canned responses for tests."""
import json
import time
from dataclasses import dataclass, field
from typing import Protocol

from loguru import logger
import asyncio

from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, AsyncOpenAI, RateLimitError

SARVAM_V1 = "https://api.sarvam.ai/v1"


class LLMError(RuntimeError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResult:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    ms: int = 0

    def assistant_message(self) -> dict:
        msg: dict = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = [{"id": tc.id, "type": "function",
                                  "function": {"name": tc.name, "arguments": json.dumps(tc.arguments, ensure_ascii=False)}}
                                 for tc in self.tool_calls]
        return msg


class ChatModel(Protocol):
    async def complete(self, messages: list[dict], tools: list[dict]) -> LLMResult: ...


def _parse_arguments(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        logger.warning(f"Unparseable tool arguments: {raw[:200]}")
        return {}


class SarvamChat:
    def __init__(self, api_key: str, model: str, timeout: float = 8.0, base_url: str = SARVAM_V1):
        self.model = model
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0,
                                  default_headers={"api-subscription-key": api_key})

    async def complete(self, messages: list[dict], tools: list[dict]) -> LLMResult:
        started = time.monotonic()
        kwargs: dict = {"model": self.model, "messages": messages, "temperature": 0.2, "max_tokens": 400}
        if tools:
            kwargs["tools"] = tools
            # sarvam-105b-conversations sometimes returns neither text nor a tool call under "auto";
            # requiring a tool (plain replies go through the `say` tool) removed that in testing.
            kwargs["tool_choice"] = "required"
        msg = None
        for attempt in range(2):   # one retry if the reply is empty or the API hiccups
            try:
                resp = await self.client.chat.completions.create(**kwargs)
            except (RateLimitError, APIConnectionError) as e:
                if attempt == 0:
                    logger.info(f"LLM {type(e).__name__}; retrying once")
                    await asyncio.sleep(0.4)
                    continue
                raise LLMError(f"{type(e).__name__}: {e}") from e
            except APIStatusError as e:
                if attempt == 0 and e.status_code >= 500:
                    await asyncio.sleep(0.4)
                    continue
                raise LLMError(f"{e.status_code}: {e}") from e
            except (APIError, APITimeoutError) as e:
                raise LLMError(str(e)) from e
            except Exception as e:  # network errors surface as various httpx exceptions
                raise LLMError(f"{type(e).__name__}: {e}") from e
            if not resp.choices:
                raise LLMError("empty response")
            msg = resp.choices[0].message
            if msg.content or msg.tool_calls:
                break
            logger.info("Empty LLM reply; retrying once")
        if msg is None:
            raise LLMError("no response")
        calls = [ToolCall(id=tc.id or f"call_{i}", name=tc.function.name, arguments=_parse_arguments(tc.function.arguments))
                 for i, tc in enumerate(msg.tool_calls or [])]
        return LLMResult(content=(msg.content or "").strip() or None, tool_calls=calls,
                         ms=int((time.monotonic() - started) * 1000))


def make_brain_llm(settings) -> "ChatModel | None":
    """The brain's model per settings.brain_provider, or None when no key is set."""
    provider = settings.brain_provider
    if provider == "anthropic":
        from .claude_llm import ClaudeChat

        return ClaudeChat(settings.anthropic_api_key, settings.anthropic_model, timeout=settings.llm_timeout)
    if provider == "sarvam":
        return SarvamChat(settings.sarvam_api_key, settings.llm_model, timeout=settings.llm_timeout)
    return None


class ScriptedLLM:
    """Test double: each call pops the next response (an LLMResult or an Exception)."""

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.requests: list[dict] = []

    async def complete(self, messages: list[dict], tools: list[dict]) -> LLMResult:
        self.requests.append({"messages": messages, "tools": tools})
        if not self.responses:
            return LLMResult(content="ठीक है।")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def call(name: str, **arguments) -> ToolCall:
    """Shorthand for building scripted tool calls in tests."""
    return ToolCall(id=f"call_{name}_{len(arguments)}", name=name, arguments=arguments)
