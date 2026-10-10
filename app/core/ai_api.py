"""Unified AI provider interface for 5 free-API models."""
from __future__ import annotations
from pathlib import Path
from typing import Optional
import os, json, urllib.request

class AIProvider:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("AI_API_KEY", "")
    def generate(self, prompt: str, max_tokens: int = 512) -> str:
        raise NotImplementedError
    def analyze(self, text: str) -> str:
        raise NotImplementedError

class OpenAIProvider(AIProvider):
    def generate(self, prompt: str, max_tokens: int = 512) -> str:
        if not self.api_key: return "[OpenAI] no key"
        try:
            req = urllib.request.Request(
                "https://api.openai.com/v1/chat/completions",
                data=json.dumps({"model":"gpt-4o-mini","messages":[{"role":"user","content":prompt}],"max_tokens":max_tokens}).encode(),
                headers={"Authorization":f"Bearer {self.api_key}","Content-Type":"application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read()).get("choices",[{}])[0].get("message",{}).get("content","")
        except Exception as exc:
            return f"[OpenAI error: {exc}]"
    def analyze(self, text: str) -> str:
        return self.generate(f"Analyze this screen content: {text[:2000]}")

class GeminiProvider(AIProvider):
    def generate(self, prompt: str, max_tokens: int = 512) -> str:
        if not self.api_key: return "[Gemini] no key"
        try:
            req = urllib.request.Request(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={self.api_key}",
                data=json.dumps({"contents":[{"parts":[{"text":prompt}]}],"generationConfig":{"maxOutputTokens":max_tokens}}).encode(),
                headers={"Content-Type":"application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read()).get("candidates",[{}])[0].get("content",{}).get("parts",[{}])[0].get("text","")
        except Exception as exc:
            return f"[Gemini error: {exc}]"
    def analyze(self, text: str) -> str:
        return self.generate(f"Analyze this screen content: {text[:2000]}")

class ClaudeProvider(AIProvider):
    def generate(self, prompt: str, max_tokens: int = 512) -> str:
        if not self.api_key: return "[Claude] no key"
        try:
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=json.dumps({"model":"claude-3-haiku-20240307","max_tokens":max_tokens,"messages":[{"role":"user","content":prompt}]}).encode(),
                headers={"x-api-key":self.api_key,"anthropic-version":"2023-06-01","Content-Type":"application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read()).get("content",[{}])[0].get("text","")
        except Exception as exc:
            return f"[Claude error: {exc}]"
    def analyze(self, text: str) -> str:
        return self.generate(f"Analyze this screen content: {text[:2000]}")

class GrokProvider(AIProvider):
    def generate(self, prompt: str, max_tokens: int = 512) -> str:
        if not self.api_key: return "[Grok] no key"
        try:
            req = urllib.request.Request(
                "https://api.x.ai/v1/chat/completions",
                data=json.dumps({"model":"grok-beta","messages":[{"role":"user","content":prompt}],"max_tokens":max_tokens}).encode(),
                headers={"Authorization":f"Bearer {self.api_key}","Content-Type":"application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read()).get("choices",[{}])[0].get("message",{}).get("content","")
        except Exception as exc:
            return f"[Grok error: {exc}]"
    def analyze(self, text: str) -> str:
        return self.generate(f"Analyze this screen content: {text[:2000]}")

class QwenProvider(AIProvider):
    def generate(self, prompt: str, max_tokens: int = 512) -> str:
        if not self.api_key: return "[Qwen] no key"
        try:
            req = urllib.request.Request(
                "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation",
                data=json.dumps({"model":"qwen-max","input":{"messages":[{"role":"user","content":prompt}]},"parameters":{"max_tokens":max_tokens}}).encode(),
                headers={"Authorization":f"Bearer {self.api_key}","Content-Type":"application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read()).get("output",{}).get("text","")
        except Exception as exc:
            return f"[Qwen error: {exc}]"
    def analyze(self, text: str) -> str:
        return self.generate(f"Analyze this screen content: {text[:2000]}")

PROVIDERS = {
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
    "claude": ClaudeProvider,
    "grok": GrokProvider,
    "qwen": QwenProvider,
}

def provider_for(name: str, api_key: str | None = None) -> AIProvider:
    cls = PROVIDERS.get(name.lower(), GeminiProvider)
    return cls(api_key=api_key)

# Multilingual prompt support
def prompt_for(model: str, lang: str = 'fa', text: str = '') -> str:
    if lang == 'fa':
        return f'لطفاً این صفحه را تحلیل کنید: {text}'
    return f'Analyze this page: {text}'
