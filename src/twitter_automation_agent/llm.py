from __future__ import annotations

import httpx
from twitter_automation_agent.config import Settings

class LLMClient:
    def __init__(self, settings: Settings, timeout: float = 60.0) -> None:
        self.settings = settings
        self.timeout = timeout

    def generate(self, prompt: str, json_format: bool = False, temperature: float = 0.35, max_tokens: int = 500, provider_override: str | None = None) -> str | None:
        provider = (provider_override or self.settings.llm_provider).lower().strip()
        if provider == "ollama":
            return self._generate_ollama(prompt, json_format, temperature, max_tokens)
        elif provider in {"huggingface", "hf"}:
            return self._generate_huggingface(prompt, temperature, max_tokens)
        elif provider == "openrouter":
            return self._generate_openrouter(prompt, json_format, temperature, max_tokens)
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

    def _generate_openrouter(self, prompt: str, json_format: bool, temperature: float, max_tokens: int) -> str | None:
        if not self.settings.openrouter_api_key:
            return None
            
        payload = {
            "model": self.settings.openrouter_model,
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        
        if json_format:
            payload["response_format"] = {"type": "json_object"}
            
        try:
            response = httpx.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.settings.openrouter_api_key}",
                    "HTTP-Referer": "https://github.com",
                    "X-Title": "TwitterAutomation",
                },
                json=payload,
                timeout=self.timeout,
                trust_env=False,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return None
            
        data = response.json()
        choices = data.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content")
        return None

    def transcribe_audio_huggingface(self, audio_data: bytes) -> str | None:
        """Transcribes OGG audio data using HuggingFace Inference API with Whisper."""
        if not self.settings.huggingface_api_token:
            return None
            
        try:
            response = httpx.post(
                "https://api-inference.huggingface.co/models/openai/whisper-large-v3-turbo",
                headers={"Authorization": f"Bearer {self.settings.huggingface_api_token}"},
                content=audio_data,
                timeout=self.timeout,
                trust_env=False,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return None

        data = response.json()
        return data.get("text")

    def transcribe_audio_local(self, audio_data: bytes) -> str | None:
        """Transcribes OGG audio data locally using SpeechRecognition and pydub. Requires ffmpeg installed on the system."""
        try:
            import speech_recognition as sr
            from pydub import AudioSegment
            import io
            
            # Convert OGG to WAV using pydub
            ogg_io = io.BytesIO(audio_data)
            audio_segment = AudioSegment.from_file(ogg_io, format="ogg")
            
            # Pad with 500ms of silence on both ends to help Google recognize short words like '3'
            silence = AudioSegment.silent(duration=500)
            padded_audio = silence + audio_segment + silence
            
            wav_io = io.BytesIO()
            padded_audio.export(wav_io, format="wav")
            wav_io.seek(0)
            
            # Use SpeechRecognition
            recognizer = sr.Recognizer()
            with sr.AudioFile(wav_io) as source:
                audio_record = recognizer.record(source)
                
            return recognizer.recognize_google(audio_record)
        except ImportError:
            raise RuntimeError("Please install SpeechRecognition and pydub to use local transcription.")
        except Exception as e:
            print(f"Local transcription failed: {e}")
            return None

    def dialog_manager_step(self, current_slots: dict, user_input: str) -> dict:
        """Parses user input into slots and generates a reply if info is missing."""
        import json
        
        slots_json = json.dumps(current_slots)
        
        prompt = (
            f"You are an AI conversational agent acting as a Dialog Manager for a Twitter bot.\n"
            f"Your goal is to extract exactly 3 slots from the user: 'action', 'topic', and 'count'.\n\n"
            f"RULES FOR SLOTS:\n"
            f"1. action: Must be either 'tweet' (exactly 1 post) or 'thread' (2 to 4 posts).\n"
            f"2. topic: The exact, corrected entity or subject of the post. You MUST correct any obvious voice-to-text misspellings (e.g., correct 'SK Highness' to 'SK Hynix'). Strip all conversational filler and keep only the core search keywords.\n"
            f"3. count: The number of tweets. This is ONLY REQUIRED if action is 'thread'. Must be an integer between 2 and 4.\n\n"
            f"CURRENT STATE:\n"
            f"Slots already extracted in previous turns: {slots_json}\n"
            f"User's new message: '{user_input}'\n\n"
            f"YOUR TASK:\n"
            f"1. Update the slots using the new message. DO NOT erase existing slot values unless the user explicitly changed them.\n"
            f"2. Determine if the required slots are fully satisfied:\n"
            f"   - If 'action' is null, it is NOT satisfied.\n"
            f"   - If 'topic' is null, it is NOT satisfied.\n"
            f"   - If 'action' is 'thread' and 'count' is null, it is NOT satisfied.\n"
            f"   - If the user provides contradicting instructions (e.g., 'post a thread of 1 tweet' or 'post a tweet of 4 tweets'), it is NOT satisfied.\n"
            f"3. If NOT satisfied, you MUST generate a conversational 'reply' asking the user for the missing information or asking them to clarify any contradictions. Ask ONLY for what is missing or contradictory.\n"
            f"4. If FULLY satisfied, set 'reply' to null.\n\n"
            f"Return ONLY a raw JSON object (no markdown, no backticks) with exactly these keys:\n"
            f'{{"updated_slots": {{"action": string or null, "topic": string or null, "count": integer or null}}, "reply": string or null}}'
        )
        
        result = self.generate(
            prompt, 
            json_format=True, 
            provider_override=self.settings.dialog_llm_provider
        )
        if not result:
            print(f"[ERROR] LLM generation failed for provider '{self.settings.dialog_llm_provider}'.")
            return {"updated_slots": current_slots, "reply": "I'm having trouble thinking right now. Please try again."}
            
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            import re
            json_match = re.search(r'\{.*\}', result, re.DOTALL)
            if json_match:
                try:
                    return json.loads(json_match.group(0))
                except Exception:
                    pass
            return {"updated_slots": current_slots, "reply": "I didn't quite catch that. Could you clarify?"}

