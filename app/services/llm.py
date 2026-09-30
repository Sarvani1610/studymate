"""LLM providers behind one small interface.

    complete(system, prompt, task=..., json_mode=False) -> LLMResult
    stream(system, prompt, task=...) -> iterator of text pieces

`stub` is an extractive provider that never calls a model. It picks the
sentences from the retrieved passages that best overlap with the question and
cites them. It keeps the whole platform usable offline, makes tests
deterministic, and is a decent fallback when the real provider is down.
"""
import json
import logging
import re
import time
from dataclasses import dataclass

from flask import current_app

from ..errors import UpstreamError
from ..metrics import LLM_CALLS, LLM_LATENCY, LLM_TOKENS
from ..utils.text import content_words, estimate_tokens, split_sentences
from .embeddings import light_stem

log = logging.getLogger(__name__)

PASSAGE_RE = re.compile(r"^\[(\d+)\]\s*(?:\([^)]*\)\s*)?(.*)$")
QUESTION_RE = re.compile(r"^Question:\s*(.*)$", re.M)
NO_ANSWER = "I could not find this in your course materials. Try rephrasing, or upload the lecture that covers it."


@dataclass
class LLMResult:
    text: str
    tokens_in: int
    tokens_out: int
    model: str
    provider: str
    latency_ms: int = 0


class BaseLLM:
    name = "base"
    model = "unknown"
    generative = True

    def complete(self, system, prompt, task="chat", json_mode=False, max_tokens=None, temperature=None):
        started = time.perf_counter()
        try:
            result = self._complete(system, prompt, json_mode, max_tokens, temperature)
        except Exception as exc:
            LLM_CALLS.labels(self.name, task, "error").inc()
            log.exception("llm call failed provider=%s task=%s", self.name, task)
            raise UpstreamError("The AI model is not responding right now. Please try again.") from exc
        elapsed = time.perf_counter() - started
        result.latency_ms = int(elapsed * 1000)
        LLM_CALLS.labels(self.name, task, "ok").inc()
        LLM_LATENCY.labels(self.name, task).observe(elapsed)
        LLM_TOKENS.labels(self.name, "in").inc(result.tokens_in)
        LLM_TOKENS.labels(self.name, "out").inc(result.tokens_out)
        return result

    def complete_json(self, system, prompt, task="json", max_tokens=None):
        result = self.complete(system, prompt, task=task, json_mode=True, max_tokens=max_tokens, temperature=0.3)
        return parse_json_loose(result.text), result

    def stream(self, system, prompt, task="chat", max_tokens=None, temperature=None, out=None):
        """Default streaming falls back to a single completion split into words."""
        result = self.complete(system, prompt, task=task, max_tokens=max_tokens, temperature=temperature)
        if out is not None:
            out["result"] = result
        yield from re.findall(r"\S+\s*", result.text)

    def _complete(self, system, prompt, json_mode, max_tokens, temperature):
        raise NotImplementedError


class StubLLM(BaseLLM):
    name = "stub"
    model = "extractive-v1"
    generative = False

    def _complete(self, system, prompt, json_mode, max_tokens, temperature):
        passages = []
        for line in prompt.splitlines():
            match = PASSAGE_RE.match(line.strip())
            if match:
                passages.append((int(match.group(1)), match.group(2)))
        q = QUESTION_RE.search(prompt)
        question = q.group(1) if q else prompt[-300:]
        text = extractive_answer(question, passages)
        return LLMResult(text, estimate_tokens(system + prompt), estimate_tokens(text), self.model, self.name)


def extractive_answer(question, passages, max_sentences=3):
    q_terms = {light_stem(w) for w in content_words(question)}
    if not q_terms or not passages:
        return NO_ANSWER
    # Bonus for sentences that define one of the question's terms ("A heap is ...").
    raw_terms = [re.escape(w) for w in content_words(question)]
    def_pattern = (
        re.compile(rf"\b({'|'.join(raw_terms)})\w*\s+(is|are|refers to|means|is defined as)\b", re.I)
        if raw_terms else None
    )
    scored = []
    for p_index, (number, text) in enumerate(passages):
        for s_index, sentence in enumerate(split_sentences(text)):
            terms = {light_stem(w) for w in content_words(sentence)}
            overlap = len(q_terms & terms)
            if overlap == 0:
                continue
            definitional = 0.5 if def_pattern and def_pattern.search(sentence) else 0.0
            score = overlap / (len(q_terms) ** 0.5) + definitional - 0.05 * p_index - 0.01 * s_index
            scored.append((score, p_index, s_index, number, sentence))
    if not scored:
        return NO_ANSWER
    ranked = sorted(scored, reverse=True)
    # Only keep sentences that are nearly as relevant as the best one, so a single
    # shared word like "time" does not drag in unrelated facts.
    cutoff = ranked[0][0] * 0.6
    best = [item for item in ranked if item[0] >= cutoff][:max_sentences]
    best.sort(key=lambda item: (item[1], item[2]))
    return " ".join(f"{sentence} [{number}]" for _, _, _, number, sentence in best)


class OpenAILLM(BaseLLM):
    name = "openai"

    def __init__(self, model, api_key, timeout, default_max_tokens, default_temperature):
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, timeout=timeout, max_retries=2)
        self.model = model
        self.default_max_tokens = default_max_tokens
        self.default_temperature = default_temperature

    def _messages(self, system, prompt):
        return [{"role": "system", "content": system}, {"role": "user", "content": prompt}]

    def _complete(self, system, prompt, json_mode, max_tokens, temperature):
        kwargs = {
            "model": self.model,
            "messages": self._messages(system, prompt),
            "max_tokens": max_tokens or self.default_max_tokens,
            "temperature": self.default_temperature if temperature is None else temperature,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = self.client.chat.completions.create(**kwargs)
        usage = resp.usage
        return LLMResult(
            resp.choices[0].message.content or "",
            usage.prompt_tokens if usage else estimate_tokens(prompt),
            usage.completion_tokens if usage else 0,
            self.model,
            self.name,
        )

    def stream(self, system, prompt, task="chat", max_tokens=None, temperature=None, out=None):
        started = time.perf_counter()
        parts = []
        usage = None
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=self._messages(system, prompt),
                max_tokens=max_tokens or self.default_max_tokens,
                temperature=self.default_temperature if temperature is None else temperature,
                stream=True,
                stream_options={"include_usage": True},
            )
            for event in resp:
                if getattr(event, "usage", None):
                    usage = event.usage
                if event.choices and event.choices[0].delta and event.choices[0].delta.content:
                    piece = event.choices[0].delta.content
                    parts.append(piece)
                    yield piece
        except Exception as exc:
            LLM_CALLS.labels(self.name, task, "error").inc()
            raise UpstreamError("The AI model stopped responding mid answer.") from exc
        text = "".join(parts)
        elapsed = time.perf_counter() - started
        LLM_CALLS.labels(self.name, task, "ok").inc()
        LLM_LATENCY.labels(self.name, task).observe(elapsed)
        if out is not None:
            out["result"] = LLMResult(
                text,
                usage.prompt_tokens if usage else estimate_tokens(system + prompt),
                usage.completion_tokens if usage else estimate_tokens(text),
                self.model,
                self.name,
                int(elapsed * 1000),
            )


class BedrockLLM(BaseLLM):
    """Anthropic models on Amazon Bedrock, using the messages API body format."""

    name = "bedrock"

    def __init__(self, model_id, region, default_max_tokens, default_temperature):
        import boto3
        from botocore.config import Config as BotoConfig

        self.client = boto3.client(
            "bedrock-runtime", region_name=region,
            config=BotoConfig(read_timeout=60, retries={"max_attempts": 4, "mode": "adaptive"}),
        )
        self.model = model_id
        self.default_max_tokens = default_max_tokens
        self.default_temperature = default_temperature

    def _body(self, system, prompt, max_tokens, temperature, json_mode):
        if json_mode:
            prompt += "\n\nRespond with a single JSON object and nothing else."
        return json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "system": system,
            "max_tokens": max_tokens or self.default_max_tokens,
            "temperature": self.default_temperature if temperature is None else temperature,
            "messages": [{"role": "user", "content": prompt}],
        })

    def _complete(self, system, prompt, json_mode, max_tokens, temperature):
        resp = self.client.invoke_model(
            modelId=self.model, body=self._body(system, prompt, max_tokens, temperature, json_mode)
        )
        payload = json.loads(resp["body"].read())
        text = "".join(block.get("text", "") for block in payload.get("content", []))
        usage = payload.get("usage", {})
        return LLMResult(text, usage.get("input_tokens", 0), usage.get("output_tokens", 0), self.model, self.name)

    def stream(self, system, prompt, task="chat", max_tokens=None, temperature=None, out=None):
        started = time.perf_counter()
        parts, tokens_in, tokens_out = [], 0, 0
        try:
            resp = self.client.invoke_model_with_response_stream(
                modelId=self.model, body=self._body(system, prompt, max_tokens, temperature, False)
            )
            for event in resp["body"]:
                chunk = json.loads(event["chunk"]["bytes"])
                kind = chunk.get("type")
                if kind == "content_block_delta":
                    piece = chunk.get("delta", {}).get("text", "")
                    if piece:
                        parts.append(piece)
                        yield piece
                elif kind == "message_start":
                    tokens_in = chunk.get("message", {}).get("usage", {}).get("input_tokens", 0)
                elif kind == "message_delta":
                    tokens_out = chunk.get("usage", {}).get("output_tokens", tokens_out)
        except Exception as exc:
            LLM_CALLS.labels(self.name, task, "error").inc()
            raise UpstreamError("The AI model stopped responding mid answer.") from exc
        elapsed = time.perf_counter() - started
        LLM_CALLS.labels(self.name, task, "ok").inc()
        LLM_LATENCY.labels(self.name, task).observe(elapsed)
        if out is not None:
            out["result"] = LLMResult("".join(parts), tokens_in, tokens_out, self.model, self.name, int(elapsed * 1000))


def parse_json_loose(text):
    """Models sometimes wrap JSON in code fences or add a sentence before it."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = min([i for i in (text.find("{"), text.find("[")) if i != -1], default=-1)
    if start == -1:
        raise UpstreamError("The AI model returned something that was not JSON")
    closing = "}" if text[start] == "{" else "]"
    end = text.rfind(closing)
    try:
        return json.loads(text[start:end + 1])
    except ValueError as exc:
        raise UpstreamError("The AI model returned malformed JSON") from exc


def get_llm():
    app = current_app
    llm = app.extensions.get("llm")
    if llm is None:
        cfg = app.config
        provider = cfg["LLM_PROVIDER"]
        if provider == "openai":
            llm = OpenAILLM(cfg["LLM_MODEL"], cfg["OPENAI_API_KEY"], cfg["LLM_TIMEOUT_S"],
                            cfg["LLM_MAX_TOKENS"], cfg["LLM_TEMPERATURE"])
        elif provider == "bedrock":
            llm = BedrockLLM(cfg["BEDROCK_MODEL_ID"], cfg["AWS_REGION"], cfg["LLM_MAX_TOKENS"], cfg["LLM_TEMPERATURE"])
        else:
            llm = StubLLM()
        app.extensions["llm"] = llm
    return llm
