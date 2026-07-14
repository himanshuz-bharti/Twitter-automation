from __future__ import annotations

import httpx
from twitter_automation_agent.config import Settings

class LLMClient:
    def __init__(self, settings: Settings, timeout: float = 60.0) -> None:
        self.settings = settings
        self.timeout = timeout

    def generate(self, prompt: str, json_format: bool = False, temperature: float = 0.35, max_tokens: int = 500) -> str | None:
        provider = self.settings.llm_provider.lower().strip()
        if provider == "ollama":
            return self._generate_ollama(prompt, json_format, temperature, max_tokens)
        elif provider in {"huggingface", "hf"}:
            return self._generate_huggingface(prompt, temperature, max_tokens)
        return None

    def _generate_ollama(self, prompt: str, json_format: bool, temperature: float, max_tokens: int) -> str | None:
        payload = {
            "model": self.settings.ollama_model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if json_format:
            payload["format"] = "json"
            
        try:
            response = httpx.post(
                f"{self.settings.ollama_base_url.rstrip('/')}/api/generate",
                json=payload,
                timeout=self.timeout,
                trust_env=False,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return None
            
        data = response.json()
        return data.get("response")

    def _generate_huggingface(self, prompt: str, temperature: float, max_tokens: int) -> str | None:
        if not self.settings.huggingface_api_token:
            return None
            
        try:
            response = httpx.post(
                f"https://api-inference.huggingface.co/models/{self.settings.huggingface_model}",
                headers={"Authorization": f"Bearer {self.settings.huggingface_api_token}"},
                json={
                    "inputs": f"<s>[INST] {prompt} [/INST]",
                    "parameters": {
                        "max_new_tokens": max_tokens,
                        "temperature": temperature,
                        "return_full_text": False,
                    },
                },
                timeout=self.timeout,
                trust_env=False,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return None

        data = response.json()
        if isinstance(data, list) and data:
            return data[0].get("generated_text")
        if isinstance(data, dict):
            return data.get("generated_text")
        return None
