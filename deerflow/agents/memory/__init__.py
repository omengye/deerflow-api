"""Memory module for DeerFlow.

This module provides a global memory mechanism that:
- Stores user context and conversation history in memory.json
- Uses LLM to summarize and extract facts from conversations
- Injects relevant memory into system prompts for personalized responses
"""

from deerflow.agents.memory.manager import (
    MemoryManager,
    MemoryOperationUnsupported,
    get_memory_manager,
    memory_manager_lease,
    probe_memory_manager_config,
    reset_memory_manager,
    validate_memory_manager_config,
)
from deerflow.agents.memory.prompt import (
    FACT_EXTRACTION_PROMPT,
    MEMORY_UPDATE_PROMPT,
    format_conversation_for_update,
    format_memory_for_injection,
)
from deerflow.agents.memory.queue import (
    ConversationContext,
    MemoryUpdateQueue,
    get_memory_queue,
    reset_memory_queue,
)
from deerflow.agents.memory.storage import (
    FileMemoryStorage,
    MemoryStorage,
    get_memory_storage,
    reset_memory_storage,
)
from deerflow.agents.memory.updater import (
    MemoryUpdater,
    clear_memory_data,
    delete_memory_fact,
    get_memory_data,
    reload_memory_data,
    update_memory_from_conversation,
)

__all__ = [
    # Prompt utilities
    "MEMORY_UPDATE_PROMPT",
    "FACT_EXTRACTION_PROMPT",
    "format_memory_for_injection",
    "format_conversation_for_update",
    # Queue
    "ConversationContext",
    "MemoryUpdateQueue",
    "get_memory_queue",
    "reset_memory_queue",
    # Backend-neutral manager
    "MemoryManager",
    "MemoryOperationUnsupported",
    "get_memory_manager",
    "memory_manager_lease",
    "probe_memory_manager_config",
    "reset_memory_manager",
    "validate_memory_manager_config",
    # Storage
    "MemoryStorage",
    "FileMemoryStorage",
    "get_memory_storage",
    "reset_memory_storage",
    # Updater
    "MemoryUpdater",
    "clear_memory_data",
    "delete_memory_fact",
    "get_memory_data",
    "reload_memory_data",
    "update_memory_from_conversation",
]
