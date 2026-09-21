"""
Per-channel anonymizer registry.
"""
import threading
from typing import Dict, Optional

from .anonymizer import Anonymizer
from ..config import settings
from .dictionary_preloader import dictionary_preloader


class AnonymizerRegistry:
    """Manages per-channel Anonymizer instances (like ChannelRegistry)."""

    _registry: Dict[str, Anonymizer] = {}
    _lock = threading.Lock()

    @classmethod
    def get(cls, channel: str) -> Anonymizer:
        """Get or create per-channel Anonymizer instance."""
        with cls._lock:
            if channel not in cls._registry:
                cls._registry[channel] = Anonymizer(
                    radical_mode=settings.anonymization_radical_mode,
                    include_errors=settings.anonymization_include_errors,
                    tokenmap_max=settings.anonymization_tokenmap_max,
                )
            return cls._registry[channel]

    @classmethod
    async def ensure_dictionary_loaded(cls, channel: str) -> None:
        """Ensure this channel's anonymizer uses the current dictionary matcher.

        Called from async context before anonymization. Never blocks: the
        preloader returns None while the background load/revalidation is in
        progress, and the previously installed matcher (if any) keeps working.
        No-op if dictionary feature is disabled.
        """
        anon = cls.get(channel)
        matcher = await dictionary_preloader.get_matcher(channel)
        if matcher is not None and anon._dict_matcher is not matcher:
            anon.set_dict_matcher(matcher)

    @classmethod
    def get_if_exists(cls, channel: str) -> Optional["Anonymizer"]:
        """Return anonymizer for channel if it exists, None otherwise."""
        with cls._lock:
            return cls._registry.get(channel)

    @classmethod
    def clear(cls, channel: Optional[str] = None):
        """Clear mapping for channel (or all channels)."""
        with cls._lock:
            if channel:
                cls._registry.pop(channel, None)
            else:
                cls._registry.clear()
