"""CADL LLM Client — Claude API wrapper for CADL generation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ClientConfig:
    """Configuration for the Claude API client."""
    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 4096
    temperature: float = 0.0
    api_key: Optional[str] = None


class ClaudeClient:
    """Wrapper around the Anthropic Claude API for CADL generation."""

    def __init__(self, config: Optional[ClientConfig] = None) -> None:
        self.config = config or ClientConfig()
        self._client = None

    def _get_client(self):
        """Lazily initialize the Anthropic client."""
        if self._client is None:
            try:
                import anthropic
            except ImportError:
                raise ImportError(
                    "The 'anthropic' package is required for AI features. "
                    "Install it with: pip install cadl[ai]"
                )
            api_key = self.config.api_key or os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise ValueError(
                    "ANTHROPIC_API_KEY environment variable is not set. "
                    "Set it or pass api_key to ClientConfig."
                )
            self._client = anthropic.Anthropic(api_key=api_key)
        return self._client

    def generate(self, system_prompt: str, user_message: str) -> str:
        """Call Claude API and return the text response.

        Args:
            system_prompt: The system prompt with CADL syntax reference and examples.
            user_message: The user's request (NL description or retry prompt).

        Returns:
            The generated text from Claude.
        """
        client = self._get_client()
        response = client.messages.create(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        return response.content[0].text

    def generate_with_history(
        self, system_prompt: str, messages: List[dict]
    ) -> str:
        """Call Claude API with conversation history (for retry).

        Args:
            system_prompt: The system prompt.
            messages: List of {"role": "user"|"assistant", "content": str} dicts.

        Returns:
            The generated text from Claude.
        """
        client = self._get_client()
        response = client.messages.create(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            system=system_prompt,
            messages=messages,
        )
        return response.content[0].text
