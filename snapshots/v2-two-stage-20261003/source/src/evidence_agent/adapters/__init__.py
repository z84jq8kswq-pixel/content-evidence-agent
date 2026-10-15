"""Model adapters."""

from .base import AdapterConfigError, ModelAdapter, RetryableError, FatalAdapterError
from .deepseek import DeepSeekAdapter
from .stub import StubAdapter

ADAPTERS = {
    "stub": StubAdapter,
    "deepseek": DeepSeekAdapter,
}

__all__ = [
    "ADAPTERS",
    "AdapterConfigError",
    "DeepSeekAdapter",
    "FatalAdapterError",
    "ModelAdapter",
    "RetryableError",
    "StubAdapter",
]
