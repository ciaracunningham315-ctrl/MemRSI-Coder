from __future__ import annotations

import asyncio
import json
import os
from urllib.parse import urlsplit

import httpx

from evocode.config import AppConfig
from evocode.contracts import LLMResponse, ToolCall
from evocode.token_budget import TokenBudgetExceeded, budget_from_environment


class ModelError(RuntimeError):
    def __init__(self, message, *, usage=None, metadata=None, raw_output=None):
        super().__init__(message)
        # Keep response diagnostics separate from the exception text so callers
        # can persist them as structured data without accidentally recording
        # request headers or credentials.
        self.usage = dict(usage or {})
        self.metadata = dict(metadata or {})
        self.raw_output = raw_output


class ModelClient:
    def __init__(self, config: AppConfig):
        self.config = config
        self.token_budget = budget_from_environment()

    def for_config(self, config: AppConfig):
        return ModelClient(config.model_copy(deep=True))

    async def count_input_tokens(self, messages, *, role="coder", tools=None, response_format=None, max_tokens=None):
        """Ask an explicitly configured backend counter for an exact request count.

        The endpoint contract is deliberately strict: it receives the same
        messages/tools sent to chat completions and must return
        ``{"input_tokens": N, "exact": true}``. Unsupported or malformed
        responses fail closed; callers can still use their conservative local
        bound and must never label it exact.
        """
        cfg = self.config.models.evolution if role == "evolution" else self.config.models.local
        if not cfg.token_count_url:
            return None
        headers = {}
        try:
            endpoint = urlsplit(cfg.token_count_url)
            completion = urlsplit(cfg.base_url)
            same_origin = (endpoint.scheme, endpoint.hostname, endpoint.port) == (
                completion.scheme, completion.hostname, completion.port
            )
        except ValueError as exc:
            raise ModelError(f"{role} token count API URL is invalid: {exc}") from exc
        if same_origin and (key := os.getenv(cfg.api_key_env)):
            headers["Authorization"] = f"Bearer {key}"
        systems = [m['content'] for m in messages if m.get('role') == 'system']
        messages = ([{'role':'system','content':'\n\n'.join(systems)}] if systems else []) + [m for m in messages if m.get('role') != 'system']
        payload = {"model": cfg.model_id, "messages": messages}
        if tools:
            payload["tools"] = tools
        if cfg.thinking is not None:
            payload["thinking"] = {"type": cfg.thinking}
        if response_format is not None:
            payload["response_format"] = response_format
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        try:
            async with httpx.AsyncClient(timeout=cfg.timeout, trust_env=False) as client:
                response = await client.post(cfg.token_count_url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ModelError(f"{role} token count API error: {type(exc).__name__}: {exc}") from exc
        count = data.get("input_tokens") if isinstance(data, dict) else None
        if type(count) is not int or count < 0 or data.get("exact") is not True:
            raise ModelError(f"{role} token count API must return exact=true and a nonnegative integer input_tokens")
        return count

    async def complete(self, messages, *, role="coder", tools=None, response_format=None, max_tokens=None):
        messages = list(messages)
        systems = [m['content'] for m in messages if m.get('role') == 'system']
        messages = ([{'role':'system','content':'\n\n'.join(systems)}] if systems else []) + [m for m in messages if m.get('role') != 'system']
        cfg = self.config.models.evolution if role == "evolution" else self.config.models.local
        if not cfg.model_id:
            raise ModelError(f"{role} model_id 未配置，请运行 evocode doctor")
        headers = {}
        if key := os.getenv(cfg.api_key_env):
            headers["Authorization"] = f"Bearer {key}"
        body = {"model": cfg.model_id, "messages": messages, "max_tokens": max_tokens or cfg.max_output_tokens}
        if cfg.thinking is not None:
            body['thinking'] = {'type': cfg.thinking}
        if tools:
            body["tools"] = tools
        if response_format:
            body["response_format"] = response_format
        async with httpx.AsyncClient(timeout=cfg.timeout, trust_env=False) as client:
            for attempt in range(3):
                reservation_id = None
                usage = None
                diagnostics = None
                message = None
                if self.token_budget is not None:
                    estimated_input = self.token_budget.estimate_request_tokens(body)
                    estimated_total = estimated_input + int(body["max_tokens"])
                    try:
                        reservation_id = self.token_budget.reserve(
                            estimated_total, role=role, model_id=cfg.model_id
                        )
                    except TokenBudgetExceeded as exc:
                        raise ModelError(str(exc), metadata={"token_budget": exc.state}) from exc
                try:
                    response = await client.post(cfg.base_url.rstrip("/") + "/chat/completions", headers=headers, json=body)
                    if response.status_code == 400 and response_format and 'response_format_not_supported' in response.text:
                        # The provider rejected the request before running inference.
                        if reservation_id is not None:
                            self.token_budget.release(reservation_id)
                            reservation_id = None
                        if response_format.get("type") == "json_schema":
                            raise ModelError(f"{role} provider does not support the requested JSON Schema output")
                        body.pop('response_format', None)
                        body['messages'] = list(messages)
                        if body['messages'] and body['messages'][0]['role']=='system':
                            body['messages'][0] = {**body['messages'][0], 'content':body['messages'][0]['content']+'\nReturn a single valid JSON object without markdown fences.'}
                        else:
                            body['messages'].insert(0, {'role':'system','content':'Return a single valid JSON object without markdown fences.'})
                        if cfg.token_count_url:
                            exact_input = await self.count_input_tokens(
                                body['messages'], role=role, tools=body.get('tools'),
                                max_tokens=body.get('max_tokens'),
                            )
                            if exact_input is None:
                                raise ModelError(f"{role} token count API did not return an exact count for the structured-output fallback request")
                            safety_margin = max(0, int(self.config.runtime.context_safety_margin_tokens))
                            available_input = cfg.max_context_tokens - body.get('max_tokens', cfg.max_output_tokens) - safety_margin
                            if exact_input > available_input:
                                raise ModelError(
                                    f"{role} structured-output fallback input ({exact_input} tokens) exceeds the available model context ({available_input})"
                                )
                        response_format = None
                        continue
                    if response.status_code in (429, 502, 503, 504) and attempt < 2:
                        # A transient response may still have reached the provider. Keep
                        # its estimate reserved when no usage receipt is available.
                        if reservation_id is not None:
                            self.token_budget.settle(reservation_id, usage)
                            reservation_id = None
                        await asyncio.sleep(2 ** attempt)
                        continue
                    response.raise_for_status()
                    data = response.json()
                    choice = data["choices"][0]
                    message = choice["message"]
                    usage = {k: v for k, v in (data.get("usage") or {}).items() if isinstance(v, int)}
                    if reservation_id is not None:
                        self.token_budget.settle(reservation_id, usage)
                        reservation_id = None
                    response_id = (
                        data.get("id") or data.get("request_id")
                        or response.headers.get("x-request-id")
                        or response.headers.get("request-id")
                    )
                    finish_reason = choice.get("finish_reason")
                    diagnostics = {
                        "model": data.get("model", cfg.model_id),
                        "response_id": response_id,
                        "finish_reason": finish_reason,
                        "raw_message": message,
                    }
                    calls = []
                    for call in message.get("tool_calls") or []:
                        args = call["function"]["arguments"]
                        args = json.loads(args) if isinstance(args, str) else args
                        if not isinstance(args, dict):
                            raise TypeError("tool arguments must be an object")
                        calls.append(ToolCall(id=call["id"], name=call["function"]["name"], arguments=args))
                    return LLMResponse(
                        content=message.get("content") or "", tool_calls=calls, usage=usage,
                        model=data.get("model", cfg.model_id),
                        reasoning_content=message.get('reasoning_content'),
                        finish_reason=finish_reason, response_id=response_id,
                        raw_message=message,
                    )
                except httpx.HTTPStatusError as exc:
                    if reservation_id is not None:
                        self.token_budget.settle(reservation_id, usage)
                    raise ModelError(f"{role} HTTP {exc.response.status_code}: {exc.response.text[:500]}") from exc
                except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as exc:
                    if reservation_id is not None:
                        self.token_budget.settle(reservation_id, usage)
                    raise ModelError(
                        f"{role} response error: {type(exc).__name__}: {exc}",
                        usage=usage,
                        metadata=diagnostics,
                        raw_output=message,
                    ) from exc
                except ModelError:
                    if reservation_id is not None:
                        self.token_budget.settle(reservation_id, usage)
                    raise
        raise ModelError("model retries exhausted")


async def doctor(config: AppConfig) -> dict:
    checks = {}
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        for name, url, env in (
            ("local_model", config.models.local.base_url.rstrip("/") + "/models", config.models.local.api_key_env),
            ("qdrant", config.raw_rag.qdrant_url.rstrip("/") + "/collections", ""),
            ("embedding", config.raw_rag.embedding_base_url.rstrip("/") + "/models", config.raw_rag.embedding_api_key_env),
        ):
            try:
                headers = {"Authorization": f"Bearer {os.environ[env]}"} if env and os.getenv(env) else {}
                res = await client.get(url, headers=headers)
                res.raise_for_status()
                checks[name] = {"available": True, "response": res.json()}
            except (httpx.HTTPError, ValueError) as exc:
                checks[name] = {"available": False, "error": str(exc)}
    process = await asyncio.create_subprocess_exec("docker", "info", "--format", "{{.ServerVersion}}", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        checks["docker"] = {"available": process.returncode == 0, "version": stdout.decode().strip(), "error": stderr.decode().strip()}
    except TimeoutError:
        process.kill()
        await process.wait()
        checks["docker"] = {"available": False, "error": "timeout"}
    checks["evolution"] = {"configured": bool(os.getenv(config.models.evolution.api_key_env)), "remote_authorized": config.evolution.allow_remote_code_context}
    return checks
