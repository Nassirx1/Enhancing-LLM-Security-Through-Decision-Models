from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import json
import math
import os
from pathlib import Path
import time

import httpx
from dotenv import load_dotenv

from .prompts import CATALOGUE, FAKE_SECRET, INPUT_QUESTIONS, OUTPUT_QUESTIONS, SYSTEM_PROMPT

ROOT = Path(__file__).resolve().parents[1]
MODES = ("baseline", "input", "input_output")
BLOCKED = "blocked"

class ProviderError(RuntimeError):
    """Sanitized provider error; never embeds request bodies or credentials."""

@dataclass
class Settings:
    llm_model: str = "qwen/qwen-2.5-7b-instruct"
    jev_model: str = "~typesafe/jev-latest"
    decision_rule: str = field(default="highest_probability", init=False)
    tie_policy: str = field(default="block", init=False)
    max_tokens: int = 384
    seed: int = 1729

    @classmethod
    def env(cls):
        load_dotenv(ROOT / ".env")
        s = cls()
        s.llm_model = os.getenv("LLM_MODEL", s.llm_model)
        s.jev_model = os.getenv("JEV_MODEL", s.jev_model)
        return s

class OpenRouter:
    def __init__(self, settings=None):
        self.settings = settings or Settings.env()
        self.key = os.getenv("OPENROUTER_API_KEY", "")
        if not self.key:
            raise ProviderError("OPENROUTER_API_KEY is missing")
        self.client = httpx.Client(timeout=60, headers={
            "Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
        })

    def post(self, endpoint, payload):
        for attempt in range(3):
            try:
                r = self.client.post("https://openrouter.ai" + endpoint, json=payload)
            except httpx.TransportError:
                if attempt == 2:
                    raise ProviderError("Provider transport error") from None
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 504, 529) and attempt < 2:
                time.sleep(min(15, 2 ** attempt))
                continue
            if r.status_code != 200:
                raise ProviderError(f"Provider HTTP {r.status_code} at {endpoint}")
            try:
                data = r.json()
                if not isinstance(data, dict) or "error" in data:
                    raise ValueError()
                return data
            except ValueError:
                raise ProviderError("Invalid provider response") from None
        raise ProviderError("Provider retries exhausted")

    def chat(self, prompt):
        s = self.settings
        data = self.post("/api/v1/chat/completions", {
            "model": s.llm_model, "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ], "temperature": 0, "seed": s.seed, "max_tokens": s.max_tokens,
        })
        try:
            answer = data["choices"][0]["message"]["content"]
            if not isinstance(answer, str) or not answer.strip():
                raise ValueError()
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderError("Missing chatbot answer") from None
        return answer, self.metadata(data)

    def decide(self, state, questions):
        data = self.post("/api/alpha/decisions", {
            "model": self.settings.jev_model, "state": state, "questions": questions,
        })
        if not isinstance(data.get("answers"), dict):
            raise ProviderError("Missing Jev answers")
        return data["answers"], self.metadata(data)

    @staticmethod
    def metadata(data):
        return {k: data.get(k) for k in ("id", "model", "provider", "usage")}

def probability(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ProviderError("Invalid Jev probability")
    return float(value)

def input_allowed(answers, settings):
    try:
        a = answers["classification"]
        expected = set(INPUT_QUESTIONS["classification"]["criteria"])
        if a["type"] != "choice" or a["choice"] not in expected or set(a["probabilities"]) != expected:
            raise ValueError()
        p = {k: probability(v) for k, v in a["probabilities"].items()}
        if abs(sum(p.values()) - 1) > 0.02:
            raise ValueError()
        # Confidence is diagnostic only. No minimum safe probability: even 0.30
        # passes when it strictly exceeds every other category. Block exact ties.
        # Rank the returned distribution ourselves; a provider's choice label
        # can disagree with its rounded probabilities and does not control gating.
        return p["safe"] > max(v for k, v in p.items() if k != "safe")
    except (KeyError, TypeError, ValueError):
        raise ProviderError("Invalid Jev input decision") from None

def output_allowed(answers, settings):
    try:
        values = []
        for name in OUTPUT_QUESTIONS:
            a = answers[name]
            if a["type"] != "noul":
                raise ValueError()
            values.append(probability(a["noul"]))
        # Noul gives P(violation); P(no violation) = 1 - P(violation).
        # Release only when "no violation" wins every binary decision.
        return all(v < 1 - v for v in values)
    except (KeyError, TypeError, ValueError):
        raise ProviderError("Invalid Jev output decision") from None

@dataclass
class Result:
    mode: str
    response: str = BLOCKED
    blocked_stage: str | None = None
    error: str | None = None
    latency_seconds: float = 0
    decisions: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)
    candidate: str | None = None

class Chatbot:
    def __init__(self, settings=None, provider=None):
        self.settings = settings or Settings.env()
        self.provider = provider or OpenRouter(self.settings)

    def run(self, prompt, mode):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be non-empty text")
        result = Result(mode)
        started = time.perf_counter()
        stage = "input" if mode != "baseline" else "llm"
        try:
            if mode != "baseline":
                a, meta = self.provider.decide({"user_message": prompt}, INPUT_QUESTIONS)
                result.calls.append({"stage": "input", **meta})
                result.decisions["input"] = a
                if not input_allowed(a, self.settings):
                    result.blocked_stage = "input"
                    return result
            stage = "llm"
            candidate, meta = self.provider.chat(prompt)
            result.calls.append({"stage": "llm", **meta})
            result.candidate = candidate
            if mode == "input_output":
                stage = "output"
                a, meta = self.provider.decide({
                    "user_message": prompt, "candidate_answer": candidate,
                    "trusted_reference": {"protected_secret": FAKE_SECRET, "catalogue": CATALOGUE},
                }, OUTPUT_QUESTIONS)
                result.calls.append({"stage": "output", **meta})
                result.decisions["output"] = a
                if not output_allowed(a, self.settings):
                    result.blocked_stage = "output"
                    return result
            result.response = candidate
            return result
        except ProviderError as exc:
            result.error = str(exc)
            result.blocked_stage = stage + "_error"
            return result
        finally:
            result.latency_seconds = time.perf_counter() - started

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=MODES, required=True)
    parser.add_argument("--prompt", help="Single-turn prompt; omit for interactive single-turn loop")
    parser.add_argument("--metadata", action="store_true", help="Show sanitized gate metadata, never suppressed candidates")
    parser.add_argument("--inspect", action="store_true", help="Show response plus gate decisions and latency for manual testing")
    args = parser.parse_args()
    bot = Chatbot()
    def answer(prompt):
        result = bot.run(prompt, args.mode)
        if args.metadata:
            data = asdict(result)
            data.pop("candidate")
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(result.response)
            if args.inspect:
                print(f"[path={result.mode} | blocked_at={result.blocked_stage or 'none'} | {result.latency_seconds:.2f}s]")
                for stage, decisions in result.decisions.items():
                    print(f"[{stage} guard] {json.dumps(decisions, ensure_ascii=False)}")
                if result.error:
                    print(f"[error] {result.error}")
        return result
    if args.prompt is not None:
        raise SystemExit(2 if answer(args.prompt).error else 0)
    print(f"Supermarket security lab | Path: {args.mode}")
    print("Guard policy: highest probability wins; ties block; no confidence cutoff.")
    print("Each prompt is an independent single-turn test. Enter /quit to exit.")
    print("Requests use your configured OpenRouter key and consume API credits.")
    while True:
        try:
            prompt = input("You: ")
        except (EOFError, KeyboardInterrupt):
            break
        if prompt == "/quit":
            break
        if prompt.strip():
            answer(prompt)

if __name__ == "__main__":
    main()
