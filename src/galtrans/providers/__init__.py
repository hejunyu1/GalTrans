"""Translation Provider adapters with no game-file capabilities."""

from galtrans.providers.openai_compatible import (
    OpenAICompatibleChatBackend,
    OpenAICompatibleProviderError,
)

__all__ = [
    "OpenAICompatibleChatBackend",
    "OpenAICompatibleProviderError",
]
