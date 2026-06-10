"""模型后端适配层。

runtime 只关心一件事：给我一个 prompt，我拿回一段文本。
不同 provider 在 HTTP 接口、响应结构、是否支持 prompt cache 上都有差异，
这些差异都在这里被抹平成统一的 complete() 接口。
"""

import json
import time
from http.client import RemoteDisconnected
import urllib.error
import urllib.request


class FakeModelClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []
        self.supports_prompt_cache = False
        self.supports_native_tools = False
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        self.prompts.append(prompt)
        self.systems = getattr(self, "systems", [])
        self.systems.append(kwargs.get("system", ""))
        self.last_completion_metadata = {}
        if not self.outputs:
            raise RuntimeError("fake model ran out of outputs")
        raw = self.outputs.pop(0)
        tool_use = _parse_fake_tool_use(raw)
        if tool_use:
            self.last_completion_metadata["tool_use"] = tool_use
        return raw


class OllamaModelClient:
    def __init__(self, model, host, temperature, top_p, timeout):
        self.model = model
        self.host = host.rstrip("/")
        self.temperature = temperature
        self.top_p = top_p
        self.timeout = timeout
        self.supports_prompt_cache = False
        self.supports_native_tools = False
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        # Ollama 当前不支持我们这里接入的 prompt cache 语义，
        # 所以 runtime 传下来的缓存参数会被忽略。
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "raw": False,
            "think": False,
            "options": {
                "num_predict": max_new_tokens,
                "temperature": self.temperature,
                "top_p": self.top_p,
            },
        }
        request = urllib.request.Request(
            self.host + "/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama request failed with HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                "Could not reach Ollama.\n"
                "Make sure `ollama serve` is running and the model is available.\n"
                f"Host: {self.host}\n"
                f"Model: {self.model}"
            ) from exc

        if data.get("error"):
            raise RuntimeError(f"Ollama error: {data['error']}")
        return data.get("response", "")


def _normalize_versioned_base_url(base_url):
    base = str(base_url).rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base


def _extract_openai_text(data):
    if data.get("output_text"):
        return data["output_text"]

    for item in data.get("output", []):
        for content in item.get("content", []):
            if isinstance(content, dict):
                text = content.get("text")
                if text:
                    return text

    choices = data.get("choices", [])
    if choices:
        message = choices[0].get("message", {})
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if text:
                        return text

    return ""


def _extract_openai_text_from_sse(body_text):
    # Chat Completions SSE: 累积 choices[0].delta.content
    deltas = []
    for line in body_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        # Chat Completions SSE
        choices = event.get("choices", [])
        if choices:
            delta = choices[0].get("delta", {})
            content = delta.get("content")
            if isinstance(content, str):
                deltas.append(content)
            continue
        # Responses API SSE (兼容旧格式)
        event_type = event.get("type", "")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
            continue
        if event_type == "response.output_text.done":
            text = event.get("text")
            if isinstance(text, str) and text:
                return text
        part = event.get("part")
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str) and text:
                return text
        item = event.get("item")
        if isinstance(item, dict):
            text = _extract_openai_text({"output": [item]})
            if text:
                return text
        response = event.get("response")
        if isinstance(response, dict):
            text = _extract_openai_text(response)
            if text:
                return text
        text = _extract_openai_text(event)
        if text:
            return text
    if deltas:
        return "".join(deltas)
    return ""


def _extract_openai_response_from_sse(body_text):
    # Chat Completions SSE: 累积 delta 中的 content 和 tool_calls
    deltas = []
    tool_calls_by_idx = {}
    response_data = {}

    for line in body_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue

        if "choices" in event:
            choice = event["choices"][0] if event["choices"] else {}
            delta = choice.get("delta", {})
            content = delta.get("content")
            if isinstance(content, str):
                deltas.append(content)
            for tc in delta.get("tool_calls", []):
                idx = tc.get("index", 0)
                if idx not in tool_calls_by_idx:
                    tool_calls_by_idx[idx] = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                merged = tool_calls_by_idx[idx]
                if tc.get("id"):
                    merged["id"] = tc["id"]
                if tc.get("type"):
                    merged["type"] = tc["type"]
                if "function" in tc:
                    if tc["function"].get("name"):
                        merged["function"]["name"] = tc["function"]["name"]
                    if tc["function"].get("arguments"):
                        merged["function"]["arguments"] += tc["function"]["arguments"]
            if event.get("usage"):
                response_data["usage"] = event["usage"]
            continue

        # Responses API SSE (兼容旧格式)
        response = event.get("response")
        if isinstance(response, dict):
            response_data = response
            if event.get("type") == "response.completed":
                text = _extract_openai_text(response)
                if text:
                    return text, response
        event_type = event.get("type", "")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
        elif event_type == "response.output_text.done":
            text = event.get("text")
            if isinstance(text, str) and text:
                return text, response_data or {}
        else:
            text = _extract_openai_text(event)
            if text:
                return text, event

    text = "".join(deltas)
    if tool_calls_by_idx and not response_data.get("choices"):
        response_data["choices"] = [{
            "message": {
                "content": text,
                "tool_calls": [tool_calls_by_idx[k] for k in sorted(tool_calls_by_idx)],
            }
        }]
    return text, response_data


def _extract_usage_cache_details(data):
    # 把不同 OpenAI-compatible 返回里的 usage 字段整理成统一结构，
    # 让 runtime/trace/report 不需要关心 provider 细节。
    usage = data.get("usage") or {}
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    cached_tokens = int(input_details.get("cached_tokens") or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": usage.get("total_tokens"),
        "cached_tokens": cached_tokens,
        "cache_hit": cached_tokens > 0,
    }


class OpenAICompatibleModelClient:
    def __init__(self, model, base_url, api_key, temperature, timeout):
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        # 当前只在明确支持 prompt cache 语义的后端上启用这条链路，
        # 避免对不支持的后端传一个"看起来统一、其实没意义"的伪参数。
        self.supports_prompt_cache = False
        self.supports_native_tools = True
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, prompt_cache_key=None, prompt_cache_retention=None, tools=None, system=None):
        """向 OpenAI-compatible 接口发起一次模型调用。

        当 tools 不为 None 且 prompt 是 list 时，使用原生 function calling 模式；
        否则走旧的纯文本 prompt 模式。
        """
        if tools is not None and isinstance(prompt, list):
            return self._complete_native(prompt, max_new_tokens, tools, system, prompt_cache_key, prompt_cache_retention)

        return self._complete_legacy(prompt, max_new_tokens, prompt_cache_key, prompt_cache_retention)

    def _complete_legacy(self, prompt, max_new_tokens, prompt_cache_key, prompt_cache_retention):
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            "max_tokens": max_new_tokens,
            "stream": False,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        attempts = 3
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body_text = response.read().decode("utf-8")
                    headers = getattr(response, "headers", {}) or {}
                    content_type = headers.get("Content-Type", "")
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code >= 500 and attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"OpenAI-compatible request failed with HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, RemoteDisconnected) as exc:
                if attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(
                    "Could not reach the OpenAI-compatible backend.\n"
                    f"Base URL: {self.base_url}\n"
                    f"Model: {self.model}"
                ) from exc

        # 有些兼容后端返回普通 JSON，有些返回 SSE。
        # 这里两种都接住，并尽量统一抽取文本和 usage/cache 元数据。
        if content_type.startswith("text/event-stream") or body_text.lstrip().startswith("data:"):
            text, response_data = _extract_openai_response_from_sse(body_text)
            if isinstance(response_data, dict) and response_data:
                # 这些元数据会一路传回 runtime，进入 trace 和 report，
                # 用来观察 prompt cache 是否真的命中。
                self.last_completion_metadata = {
                    **_extract_usage_cache_details(response_data),
                }
            if text:
                return text
            raise RuntimeError("OpenAI-compatible error: could not extract text from event stream response")

        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "OpenAI-compatible error: backend returned non-JSON content that could not be parsed"
            ) from exc
        if data.get("error"):
            raise RuntimeError(f"OpenAI-compatible error: {data['error']}")
        self.last_completion_metadata = {
            **_extract_usage_cache_details(data),
        }
        return _extract_openai_text(data)

    def _complete_native(self, messages, max_new_tokens, tools, system, prompt_cache_key, prompt_cache_retention):
        """使用 Chat Completions API 的原生 function calling。"""
        self.last_completion_metadata = {}

        openai_messages = []
        if system:
            openai_messages.append({"role": "system", "content": system})

        for msg in messages:
            openai_messages.extend(_generic_msg_to_openai_input(msg))

        openai_tools = _tools_to_openai_format(tools)

        payload = {
            "model": self.model,
            "messages": openai_messages,
            "tools": openai_tools,
            "max_tokens": max_new_tokens,
            "stream": False,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        attempts = 3
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body_text = response.read().decode("utf-8")
                    resp_headers = getattr(response, "headers", {}) or {}
                    content_type = resp_headers.get("Content-Type", "")
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code >= 500 and attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"OpenAI-compatible request failed with HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, RemoteDisconnected) as exc:
                if attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(
                    "Could not reach the OpenAI-compatible backend.\n"
                    f"Base URL: {self.base_url}\n"
                    f"Model: {self.model}"
                ) from exc

        if content_type.startswith("text/event-stream") or body_text.lstrip().startswith("data:"):
            text, response_data = _extract_openai_response_from_sse(body_text)
            self.last_completion_metadata = {
                **_extract_usage_cache_details(response_data),
            }
            tool_use = _extract_openai_tool_use(response_data)
            if tool_use:
                self.last_completion_metadata["tool_use"] = tool_use
            if text:
                return text
            return ""

        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "OpenAI-compatible error: backend returned non-JSON content that could not be parsed"
            ) from exc
        if data.get("error"):
            raise RuntimeError(f"OpenAI-compatible error: {data['error']}")

        self.last_completion_metadata = {
            **_extract_usage_cache_details(data),
        }
        tool_use = _extract_openai_tool_use(data)
        if tool_use:
            self.last_completion_metadata["tool_use"] = tool_use
        return _extract_openai_text(data)


def _parse_fake_tool_use(raw):
    """从 <tool>...</tool> 格式的 fake 输出中提取 tool_use，供 FakeModelClient 使用。"""
    import re
    m = re.search(r"<tool>\s*(\{.*?\})\s*</tool>", raw, re.S)
    if m:
        try:
            payload = json.loads(m.group(1))
            name = str(payload.get("name", "")).strip()
            args = payload.get("args", {})
            if name and isinstance(args, dict):
                return {"name": name, "args": args, "tool_use_id": "fake-id"}
        except (json.JSONDecodeError, AttributeError):
            pass
    return None


def _extract_anthropic_text(data):
    for item in data.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str) and text:
                return text
    return ""


def _extract_openai_tool_use(data):
    """从 Chat Completions 或 Responses API 响应中提取 function_call。"""
    # Chat Completions 格式: choices[0].message.tool_calls
    choices = data.get("choices", [])
    if choices:
        message = choices[0].get("message", {})
        tool_calls = message.get("tool_calls", [])
        if tool_calls:
            tc = tool_calls[0]
            args_str = tc.get("function", {}).get("arguments", "{}")
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except json.JSONDecodeError:
                args = {}
            return {
                "name": tc.get("function", {}).get("name", ""),
                "args": args,
                "tool_use_id": tc.get("id", ""),
            }
    # Responses API 格式 (兼容旧格式): output 数组中的 function_call
    for item in data.get("output", []):
        if isinstance(item, dict) and item.get("type") == "function_call":
            args_str = item.get("arguments", "{}")
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except json.JSONDecodeError:
                args = {}
            return {
                "name": item.get("name", ""),
                "args": args,
                "tool_use_id": item.get("call_id", item.get("id", "")),
            }
    return None


def _tools_to_openai_format(tools):
    """将 get_tools_for_api 返回的工具定义转为 OpenAI Responses 格式。"""
    result = []
    for tool in tools:
        result.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": {
                    **tool.get("input_schema", {}),
                    "additionalProperties": False,
                },
            },
        })
    return result


def _generic_msg_to_openai_input(msg):
    """将通用结构化消息转为 Chat Completions API 的 messages 项。

    function_call 嵌套在 assistant 消息的 tool_calls 里，
    function_call_output 转为 role=tool 消息。
    """
    role = msg.get("role", "")
    content = msg.get("content", "")
    tool_call = msg.get("tool_call")

    if role == "user":
        return [{"role": "user", "content": content}]

    if role == "assistant":
        item = {"role": "assistant", "content": content or ""}
        if tool_call:
            item["tool_calls"] = [{
                "id": tool_call.get("id", ""),
                "type": "function",
                "function": {
                    "name": tool_call.get("name", ""),
                    "arguments": json.dumps(tool_call.get("args", {}), ensure_ascii=False),
                },
            }]
        return [item]

    if role == "tool":
        return [{
            "role": "tool",
            "tool_call_id": msg.get("tool_call_id", ""),
            "content": content,
        }]

    return []


def _generic_msg_to_anthropic(msg):
    """将通用结构化消息转为 Anthropic Messages API 的消息格式。"""
    role = msg.get("role", "")
    content = msg.get("content", "")
    tool_call = msg.get("tool_call")

    if role == "user":
        return {"role": "user", "content": [{"type": "text", "text": content}]}

    if role == "assistant":
        if tool_call:
            blocks = []
            if content:
                blocks.append({"type": "text", "text": content})
            blocks.append({
                "type": "tool_use",
                "id": tool_call.get("id", ""),
                "name": tool_call.get("name", ""),
                "input": tool_call.get("args", {}),
            })
            return {"role": "assistant", "content": blocks}
        return {"role": "assistant", "content": [{"type": "text", "text": content}]}

    if role == "tool":
        return {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id", ""),
                "content": content,
            }],
        }

    return {"role": "user", "content": [{"type": "text", "text": content}]}


class AnthropicCompatibleModelClient:
    def __init__(self, model, base_url, api_key, temperature, timeout):
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.supports_prompt_cache = False
        self.supports_native_tools = True
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, prompt_cache_key=None, prompt_cache_retention=None, tools=None, system=None):
        """向 Anthropic-compatible `/messages` 接口发起一次模型调用。

        当 tools 不为 None 且 prompt 是 list 时，使用原生 function calling 模式；
        否则走旧的纯文本 prompt 模式。
        """
        del prompt_cache_key, prompt_cache_retention
        self.last_completion_metadata = {}

        if tools is not None and isinstance(prompt, list):
            return self._complete_native(prompt, max_new_tokens, tools, system)

        return self._complete_legacy(prompt, max_new_tokens)

    def _complete_legacy(self, prompt, max_new_tokens):
        self.last_completion_metadata = {}
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt,
                        }
                    ],
                }
            ],
            "max_tokens": max_new_tokens,
            "stream": False,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        request = urllib.request.Request(
            self.base_url + "/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        attempts = 3
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body_text = response.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code >= 500 and attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"Anthropic-compatible request failed with HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, RemoteDisconnected) as exc:
                if attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(
                    "Could not reach the Anthropic-compatible backend.\n"
                    f"Base URL: {self.base_url}\n"
                    f"Model: {self.model}"
                ) from exc

        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Anthropic-compatible error: backend returned non-JSON content that could not be parsed"
            ) from exc
        if data.get("error"):
            raise RuntimeError(f"Anthropic-compatible error: {data['error']}")
        text = _extract_anthropic_text(data)
        if text:
            return text
        raise RuntimeError("Anthropic-compatible error: could not extract text from response")

    def _complete_native(self, messages, max_new_tokens, tools, system):
        anthropic_messages = [_generic_msg_to_anthropic(msg) for msg in messages]
        payload = {
            "model": self.model,
            "messages": anthropic_messages,
            "max_tokens": max_new_tokens,
            "tools": tools,
            "stream": False,
        }
        if system:
            payload["system"] = system
        if self.temperature is not None:
            payload["temperature"] = self.temperature

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        request = urllib.request.Request(
            self.base_url + "/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        attempts = 3
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body_text = response.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code >= 500 and attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"Anthropic-compatible request failed with HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, RemoteDisconnected) as exc:
                if attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(
                    "Could not reach the Anthropic-compatible backend.\n"
                    f"Base URL: {self.base_url}\n"
                    f"Model: {self.model}"
                ) from exc

        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Anthropic-compatible error: backend returned non-JSON content that could not be parsed"
            ) from exc
        if data.get("error"):
            raise RuntimeError(f"Anthropic-compatible error: {data['error']}")

        usage = data.get("usage") or {}
        self.last_completion_metadata = {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "stop_reason": data.get("stop_reason", ""),
        }

        tool_use = _extract_anthropic_tool_use(data)
        if tool_use:
            self.last_completion_metadata["tool_use"] = tool_use

        text = _extract_anthropic_text(data)
        return text if text else ""

    def _send_http(self, url, payload, headers):
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        attempts = 3
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code >= 500 and attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"Anthropic-compatible request failed with HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, RemoteDisconnected) as exc:
                if attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(
                    "Could not reach the Anthropic-compatible backend.\n"
                    f"Base URL: {self.base_url}\n"
                    f"Model: {self.model}"
                ) from exc
