# scripts/lib/llm_client.py
"""
LLM Client with Hybrid Multi-Provider Support

Hybrid Strategy:
- Critical tasks (EIS, Overpotential, Verifier) → Gemini Pro
- Important tasks (Case Builder, Input, Table) → Gemini Flash
- Routine tasks (Cycling, Corrosion, Categorizer) → Ollama

Configuration via .env file or environment variables.
"""
from __future__ import annotations
import hashlib
import json
import logging
import os
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

from scripts.lib.io_jsonl import write_json, read_json

# 16_설계: LLM Tracing imports
from scripts.lib.llm_trace import TraceWriter, TraceContext, get_global_writer

# Logger for this module
logger = logging.getLogger(__name__)

# Load .env file if exists
def _load_dotenv():
    """Load environment variables from .env file."""
    env_path = Path(__file__).parent.parent.parent / ".env"
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())

_load_dotenv()

# ============================================================================
# Task-Based Model Routing (HYBRID STRATEGY)
# ============================================================================
TASK_MODEL_MAP = {
    # CRITICAL TASKS: Gemini 2.5 Pro (최고 성능, 복잡한 추론)
    "EXTRACT_EIS": {"provider": "gemini", "model": "gemini-2.5-pro", "thinking": True},
    "EXTRACT_OVERPOTENTIAL": {"provider": "gemini", "model": "gemini-2.5-flash", "thinking": True},
    "VERIFIER": {"provider": "gemini", "model": "gemini-2.5-flash", "thinking": True},
    
    # IMPORTANT TASKS: Gemini 2.5 Flash (빠르고 효율적, 구조화 작업)
    "CASE_BUILDER": {"provider": "gemini", "model": "gemini-2.5-flash", "thinking": False},
    "EXTRACT_INPUT": {"provider": "gemini", "model": "gemini-2.5-flash", "thinking": False},
    "TABLE_AGENT": {"provider": "gemini", "model": "gemini-2.5-flash", "thinking": False},
    "EXTRACT_CYCLING": {"provider": "gemini", "model": "gemini-2.5-flash", "thinking": False},
    "EXTRACT_RATE": {"provider": "gemini", "model": "gemini-2.5-pro", "thinking": True},  # Phase 4: Upgraded for complex unit parsing
    "EXTRACT_KINETICS": {"provider": "gemini", "model": "gemini-2.5-flash", "thinking": False},  # NEW: Phase 3
    
    # ROUTINE TASKS: Ollama (무료, 단순 반복 작업)
    "CATEGORIZER": {"provider": "ollama", "model": "qwen2.5:14b-instruct", "thinking": False},
    "INCLUSION": {"provider": "ollama", "model": "qwen2.5:14b-instruct", "thinking": False},
    "EXTRACT_CORROSION": {"provider": "gemini", "model": "gemini-2.5-flash", "thinking": False},
    "ORGANIZER": {"provider": "ollama", "model": "qwen2.5:14b-instruct", "thinking": False},
}

def get_model_for_task(task_type: str) -> Dict[str, Any]:
    """Get appropriate model configuration for a specific task."""
    strategy = os.environ.get("MODEL_STRATEGY", "hybrid").lower()
    
    # Override: Use only Ollama
    if strategy == "ollama_only":
        return {"provider": "ollama", "model": "qwen2.5:14b-instruct", "thinking": False}
    
    # Override: Use only Gemini
    if strategy == "gemini_only":
        return {"provider": "gemini", "model": "gemini-2.5-pro", "thinking": True}
    
    # Hybrid: Use task-specific mapping
    config = TASK_MODEL_MAP.get(task_type)
    if config:
        return config
    
    # Fallback for unknown tasks
    return {"provider": "ollama", "model": "qwen2.5:14b-instruct", "thinking": False}


# ============================================================================
# Configuration
# ============================================================================
class LLMConfig:
    """LLM configuration with sensible defaults."""
    
    # Provider selection (legacy, kept for compatibility)
    PROVIDER = os.environ.get("LLM_PROVIDER", "ollama").lower()
    
    # Ollama settings
    OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    
    # Gemini API Key
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
    
    # Default model mappings by provider (legacy tier-based)
    DEFAULT_MODELS = {
        "ollama": {
            "small": "qwen2.5:14b-instruct",
            "mid": "qwen2.5:14b-instruct",
            "large": "qwen3:30b-thinking",
        },
        "openai": {
            "small": "gpt-4o-mini",
            "mid": "gpt-4o",
            "large": "gpt-4o",
        },
        "gemini": {
            "small": "gemini-2.5-flash",
            "mid": "gemini-2.5-flash",
            "large": "gemini-2.5-pro",
        }
    }
    
    # Temperature settings
    TEMPERATURE_DEFAULT = 0.1
    TEMPERATURE_THINKING = 0.3
    
    # Timeout and retry
    TIMEOUT = 300  # seconds
    MAX_RETRIES = 2
    
    # Cost tracking
    gemini_call_count = 0
    MAX_GEMINI_CALLS = int(os.environ.get("MAX_GEMINI_CALLS_PER_RUN", "200"))


def get_model_name(tier: str) -> str:
    """
    Get model name for the given tier.
    
    Priority:
    1. Environment variable (MODEL_SMALL, MODEL_MID, MODEL_LARGE)
    2. Default for current provider
    """
    tier_lower = tier.lower()
    tier_map = {"s": "small", "m": "mid", "l": "large"}
    tier_key = tier_map.get(tier_lower, tier_lower)
    
    # Check env var first
    env_key = f"MODEL_{tier_key.upper()}"
    env_val = os.environ.get(env_key)
    if env_val:
        return env_val
    
    # Fall back to provider defaults
    provider = LLMConfig.PROVIDER
    if provider in LLMConfig.DEFAULT_MODELS:
        return LLMConfig.DEFAULT_MODELS[provider].get(tier_key, "qwen2.5:14b-instruct")
    
    return "qwen2.5:14b-instruct"


# ============================================================================
# Provider Implementations
# ============================================================================
def _call_ollama(model: str, prompt: str, thinking: bool = False) -> str:
    """
    Call Ollama API.
    
    For qwen3 models, uses /think tag for extended reasoning when thinking=True.
    """
    import requests
    
    # Use model manager for memory management
    OllamaModelManager.ensure_model_loaded(model)
    
    url = f"{LLMConfig.OLLAMA_URL}/api/generate"
    
    # For qwen3 thinking models, wrap with /think tag
    if thinking and "qwen3" in model.lower():
        prompt = f"/think\n{prompt}"
    
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": LLMConfig.TEMPERATURE_THINKING if thinking else LLMConfig.TEMPERATURE_DEFAULT,
            "num_predict": 4096,
        }
    }
    
    response = requests.post(url, json=payload, timeout=LLMConfig.TIMEOUT)
    response.raise_for_status()
    
    result = response.json()
    return result.get("response", "")


# ============================================================================
# Ollama Model Manager (Memory Management)
# ============================================================================
class OllamaModelManager:
    """
    Manage Ollama model loading/unloading to optimize VRAM usage.
    
    Features:
    - Track currently loaded model
    - Unload previous model before loading new one
    - Batch queue for grouping tasks by model tier
    """
    _current_model: str = None
    _batch_queue: list = []
    _batch_size: int = 10  # Process this many tasks before considering switch
    
    @classmethod
    def get_loaded_models(cls) -> list:
        """Get list of currently loaded models in Ollama."""
        import requests
        try:
            response = requests.get(f"{LLMConfig.OLLAMA_URL}/api/ps", timeout=10)
            if response.status_code == 200:
                data = response.json()
                return [m.get("name", "") for m in data.get("models", [])]
        except:
            pass
        return []
    
    @classmethod
    def unload_model(cls, model: str) -> bool:
        """
        Unload a model from Ollama memory.
        
        Uses keep_alive=0 to immediately unload.
        """
        import requests
        try:
            # Send a dummy request with keep_alive=0 to unload
            url = f"{LLMConfig.OLLAMA_URL}/api/generate"
            payload = {
                "model": model,
                "prompt": "",
                "keep_alive": 0  # Unload immediately
            }
            response = requests.post(url, json=payload, timeout=30)
            print(f"[ModelManager] Unloaded model: {model}")
            return response.status_code == 200
        except Exception as e:
            print(f"[ModelManager] Failed to unload {model}: {e}")
            return False
    
    @classmethod
    def unload_all_models(cls) -> None:
        """Unload all currently loaded models."""
        loaded = cls.get_loaded_models()
        for model in loaded:
            cls.unload_model(model)
        cls._current_model = None
    
    @classmethod
    def ensure_model_loaded(cls, model: str) -> None:
        """
        Ensure the specified model is loaded.
        
        If a different model is currently loaded, unload it first.
        """
        if cls._current_model == model:
            return
        
        # Unload previous model if exists
        if cls._current_model:
            print(f"[ModelManager] Switching from {cls._current_model} to {model}")
            cls.unload_model(cls._current_model)
        
        cls._current_model = model
        print(f"[ModelManager] Model ready: {model}")
    
    @classmethod
    def add_to_batch(cls, task_info: dict) -> None:
        """Add a task to the batch queue."""
        cls._batch_queue.append(task_info)
    
    @classmethod
    def get_batch_by_tier(cls, tier: str) -> list:
        """Get all tasks for a specific tier."""
        return [t for t in cls._batch_queue if t.get("tier") == tier]
    
    @classmethod
    def clear_batch(cls) -> None:
        """Clear the batch queue."""
        cls._batch_queue = []
    
    @classmethod
    def should_switch_model(cls, new_model: str) -> bool:
        """Check if we should switch models now."""
        if cls._current_model is None:
            return True
        if cls._current_model == new_model:
            return False
        # Check if batch queue has pending tasks for current model
        pending = len([t for t in cls._batch_queue if t.get("model") == cls._current_model])
        return pending == 0


def process_tasks_batched(tasks: list, process_func) -> list:
    """
    Process tasks in batches, grouped by model tier to minimize switching.
    
    Args:
        tasks: List of task dicts with "tier" key
        process_func: Function to process each task
    
    Returns:
        List of results
    """
    # Group tasks by tier
    by_tier = {"small": [], "mid": [], "large": []}
    for t in tasks:
        tier = t.get("tier", "mid")
        if tier in by_tier:
            by_tier[tier].append(t)
    
    results = []
    
    # Process in order: small -> mid -> large (ascending complexity)
    for tier in ["small", "mid", "large"]:
        tier_tasks = by_tier[tier]
        if not tier_tasks:
            continue
        
        model = get_model_name(tier)
        print(f"[Batch] Processing {len(tier_tasks)} {tier}-tier tasks with {model}")
        
        for task in tier_tasks:
            try:
                result = process_func(task)
                results.append(result)
            except Exception as e:
                print(f"[Batch] Task failed: {e}")
                results.append({"error": str(e)})
    
    # Unload model after batch complete to free memory
    OllamaModelManager.unload_all_models()
    
    return results


def _call_openai(model: str, prompt: str, thinking: bool = False) -> str:
    """Call OpenAI API."""
    import openai
    
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY environment variable not set")
    
    client = openai.OpenAI(api_key=api_key)
    
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=LLMConfig.TEMPERATURE_THINKING if thinking else LLMConfig.TEMPERATURE_DEFAULT,
        max_tokens=4096,
    )
    
    return response.choices[0].message.content


from google import genai
from google.genai import types

def _call_gemini(model: str, system_and_user_prompt: str, thinking: bool = False) -> str:
    """Call Google Gemini API using the new google-genai SDK."""
    api_key = LLMConfig.GEMINI_API_KEY
    if not api_key:
        raise ValueError("GEMINI_API_KEY not set")
    
    # Retry configuration for 503/429 errors
    max_retries = 5
    base_delay = 2.0
    
    for attempt in range(max_retries + 1):
        try:
            client = genai.Client(api_key=api_key)
            
            # Configure generation config
            config = types.GenerateContentConfig(
                temperature=0.7 if thinking else 0.1,
                top_p=0.95,
                top_k=40,
                max_output_tokens=16384,  # Phase 5: Increased from 8192 to prevent truncation
            )
            
            # Call API
            response = client.models.generate_content(
                model=model,
                contents=system_and_user_prompt,
                config=config
            )
            
            return response.text
            
        except Exception as e:
            error_str = str(e)
            is_transient = "503" in error_str or "429" in error_str or "overloaded" in error_str
            
            if is_transient and attempt < max_retries:
                sleep_time = base_delay * (2 ** attempt)
                print(f"[Gemini] Error {e}. Retrying in {sleep_time}s... (Attempt {attempt+1}/{max_retries})")
                time.sleep(sleep_time)
                continue
            
            # If exhausted retries or non-transient error, re-raise
            print(f"[Gemini] Fatal Error: {e}")
            raise


def call_provider(model: str, system_and_user_prompt: str, thinking: Optional[bool] = None) -> str:
    """
    Call LLM provider API based on configuration.
    
    Auto-detects provider from LLM_PROVIDER env var or model name prefix.
    
    Args:
        model: Model name/identifier
        system_and_user_prompt: Combined prompt text
        thinking: Enable extended thinking (uses qwen3 /think or higher temp)
    
    Returns:
        Raw text response from the model
    """
    provider = LLMConfig.PROVIDER
    
    # Auto-detect provider from model name if needed
    if model.startswith("gpt-"):
        provider = "openai"
    elif model.startswith("gemini-"):
        provider = "gemini"
    elif ":" in model:  # Ollama format like "qwen2.5:14b"
        provider = "ollama"
    
    thinking = thinking or False
    
    for attempt in range(LLMConfig.MAX_RETRIES + 1):
        try:
            if provider == "ollama":
                return _call_ollama(model, system_and_user_prompt, thinking)
            elif provider == "openai":
                return _call_openai(model, system_and_user_prompt, thinking)
            elif provider == "gemini":
                # Check Gemini call limit
                if LLMConfig.gemini_call_count >= LLMConfig.MAX_GEMINI_CALLS:
                    print(f"[CostControl] Gemini call limit reached ({LLMConfig.MAX_GEMINI_CALLS}), falling back to Ollama")
                    return _call_ollama("qwen2.5:14b-instruct", system_and_user_prompt, thinking)
                LLMConfig.gemini_call_count += 1
                return _call_gemini(model, system_and_user_prompt, thinking)
            else:
                raise ValueError(f"Unknown provider: {provider}")
        except Exception as e:
            if attempt < LLMConfig.MAX_RETRIES:
                time.sleep(2 ** attempt)  # Exponential backoff
                continue
            raise


def call_for_task(
    task_type: str, 
    prompt: str,
    trace_ctx: Optional[TraceContext] = None,
    trace_writer: Optional[TraceWriter] = None,
) -> str:
    """
    Call the appropriate model for a specific task.
    
    This is the main entry point for the hybrid model system.
    Uses task-based routing to select the best model for each task.
    
    Per 16_설계.md: Now routes through call_llm_text for 100% tracing.
    
    Args:
        task_type: One of EXTRACT_EIS, EXTRACT_OVERPOTENTIAL, CASE_BUILDER, etc.
        prompt: The full prompt text
        trace_ctx: Optional trace context for logging
        trace_writer: Optional trace writer (uses global if not provided)
    
    Returns:
        Raw text response from the model
    """
    config = get_model_for_task(task_type)
    provider = config["provider"]
    model = config["model"]
    thinking = config.get("thinking", False)
    
    print(f"[TaskRouter] {task_type} → {provider}:{model}")
    
    # 16_설계: Use traced gateway instead of direct call_provider
    return call_llm_text(
        model=model,
        provider=provider,
        rendered_prompt=prompt,
        cache_key=f"task:{task_type}",
        trace_ctx=trace_ctx,
        trace_writer=trace_writer,
        thinking=thinking,
        extra={"task_type": task_type}
    )


# ============================================================================
# 16_설계: Traced LLM Gateway for Text Responses
# ============================================================================
def call_llm_text(
    *,
    model: str,
    rendered_prompt: str,
    cache_key: str,
    provider: Optional[str] = None,
    trace_ctx: Optional[TraceContext] = None,
    trace_writer: Optional[TraceWriter] = None,
    thinking: bool = False,
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Traced gateway for all text LLM calls.
    
    Per 16_설계.md Section 2-3:
    - All text LLM calls must go through this function
    - Logs template, variables, rendered prompt, response to TraceWriter
    - Ensures 100% traceability
    
    Args:
        model: Model name
        rendered_prompt: Fully rendered prompt text
        cache_key: Cache key for deduplication
        provider: Provider name (auto-detected if not provided)
        trace_ctx: Context for tracing (paper_id, case_id, etc.)
        trace_writer: TraceWriter instance (uses global if not provided)
        thinking: Enable extended thinking mode
        extra: Additional metadata to log
    
    Returns:
        Raw text response from the model
    """
    # Use global writer if not provided
    writer = trace_writer or get_global_writer()
    call_id = writer.new_call_id() if writer else None
    t0 = int(time.time() * 1000)
    
    # Create base record
    rec = {
        "call_id": call_id,
        "ts_start_ms": t0,
        "cache_key": cache_key,
        "provider": provider,
        "model": model,
        "kind": "TEXT",
        "thinking": thinking,
        "extra": extra or {},
    }
    
    if trace_ctx:
        rec["trace_ctx"] = trace_ctx.to_dict()
        rec["paper_id"] = trace_ctx.paper_id
        rec["case_id"] = trace_ctx.case_id
        rec["stage"] = trace_ctx.stage
        rec["task_type"] = trace_ctx.task_type
        rec["attempt"] = trace_ctx.attempt
    
    try:
        # Save rendered prompt artifact
        if writer and call_id:
            rec.update(writer.maybe_externalize_text(call_id, "rendered_prompt", rendered_prompt))
        
        # Actual LLM call (low-level)
        resp = call_provider(model=model, system_and_user_prompt=rendered_prompt, thinking=thinking)
        
        t1 = int(time.time() * 1000)
        rec["ts_end_ms"] = t1
        rec["latency_ms"] = t1 - t0
        rec["ok"] = True
        rec["response_length"] = len(resp) if resp else 0
        
        # Save response artifact
        if writer and call_id:
            rec.update(writer.maybe_externalize_text(call_id, "response_raw", str(resp)))
        
        if writer:
            writer.append_call(rec)
        
        return resp
        
    except Exception as e:
        t1 = int(time.time() * 1000)
        rec["ts_end_ms"] = t1
        rec["latency_ms"] = t1 - t0
        rec["ok"] = False
        rec["error"] = {
            "type": type(e).__name__,
            "message": str(e),
            "traceback": traceback.format_exc(),
        }
        if writer:
            if call_id:
                writer.write_artifact_json(call_id, "error.json", rec["error"])
            writer.append_call(rec)
        raise


# ============================================================================
# Utility Functions
# ============================================================================
def _hash(s: str) -> str:
    """Generate a short hash for cache key."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:24]


def _load_prompt(prompt_file: str) -> str:
    """Load prompt template from file."""
    with open(prompt_file, "r", encoding="utf-8") as f:
        return f.read()


def _render(template: str, variables: Dict[str, Any]) -> str:
    """Render template with variable substitution."""
    out = template
    for k, v in variables.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def _json_parse_strict(s: str) -> Dict[str, Any]:
    """
    Parse JSON with robust handling of markdown fences and thinking output.
    
    Phase 5: Enhanced to handle JSON inside markdown code blocks anywhere
    in the response (e.g., after LLM thinking output).
    """
    import re
    
    s = s.strip()
    
    # Strategy 1: Extract from ```json ... ``` code block anywhere
    json_block_match = re.search(r'```json\s*([\s\S]*?)```', s)
    if json_block_match:
        json_str = json_block_match.group(1).strip()
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            pass  # Fall through to other strategies
    
    # Strategy 2: Extract from ``` ... ``` code block (without json tag)
    code_block_match = re.search(r'```\s*([\s\S]*?)```', s)
    if code_block_match:
        json_str = code_block_match.group(1).strip()
        # Check if it starts with { or [
        if json_str.startswith('{') or json_str.startswith('['):
            try:
                return json.loads(json_str)
            except json.JSONDecodeError:
                pass
    
    # Strategy 3: Original method - starts with ```
    if s.startswith("```"):
        first_newline = s.find("\n")
        if first_newline != -1:
            s = s[first_newline+1:]
        else:
            s = s[3:]
        
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3].rstrip()
    
    # Strategy 4: Find longest valid JSON object { ... }
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        # Find first { and last } 
        i = s.find("{")
        j = s.rfind("}")
        if i != -1 and j != -1 and j > i:
            try:
                return json.loads(s[i:j+1])
            except json.JSONDecodeError:
                pass
    
    # Strategy 5: If all else fails, raise original error
    return json.loads(s)


# ============================================================================
# Main API
# ============================================================================
def call_llm_json(
    model: str,
    prompt_file: str,
    variables: Dict[str, Any],
    cache_key: str,
    cache_dir: str = "cache/llm",
    thinking: Optional[bool] = None,
    use_cache: bool = True
) -> Dict[str, Any]:
    """
    Call LLM with caching and JSON parsing.
    
    Features:
    - Automatic caching based on cache_key + model + rendered prompt
    - JSON parsing with fallback error handling
    - Markdown fence removal
    - Thinking/output unwrapping
    
    Args:
        model: Model name (use get_model_name("small"|"mid"|"large"))
        prompt_file: Path to prompt template file
        variables: Dict of variables to substitute in template
        cache_key: Unique key for caching (e.g., paper_id:chunk_id:task)
        cache_dir: Directory for cache files
        thinking: Enable extended thinking (for L-tier tasks)
        use_cache: If False, bypass cache read (force re-run)
    
    Returns:
        Parsed JSON dict from LLM response
    """
    cache_dir_p = Path(cache_dir)
    cache_dir_p.mkdir(parents=True, exist_ok=True)

    prompt = _load_prompt(prompt_file)
    rendered = _render(prompt, variables)
    ck = _hash(cache_key + "::" + model + "::" + rendered)
    cache_path = cache_dir_p / f"{ck}.json"

    # Return cached result if available
    if use_cache and cache_path.exists():
        cached = read_json(cache_path)
        # Ensure cached result is a dict
        if isinstance(cached, dict):
            return cached
        elif isinstance(cached, list):
            return {"measurements": cached}
        else:
            # Invalid cache, will regenerate
            pass

    # Call LLM
    start_time = time.time()
    raw = call_provider(model=model, system_and_user_prompt=rendered, thinking=thinking)
    elapsed_time = time.time() - start_time

    # =========================================================================
    # LLM TRACE LOGGING (11_설계: 추적용 프롬프트/응답 저장)
    # Per-paper directory structure for easy traceability
    # =========================================================================
    
    # Extract paper_id and case_id from cache_key (format: paper_id:case_id:task)
    cache_parts = cache_key.split(":") if cache_key else []
    paper_id = cache_parts[0] if len(cache_parts) > 0 else "unknown_paper"
    case_id = cache_parts[1] if len(cache_parts) > 1 else "unknown_case"
    
    # Create per-paper trace directory
    trace_base = cache_dir_p.parent / "llm_traces"
    trace_dir = trace_base / paper_id
    trace_dir.mkdir(parents=True, exist_ok=True)
    
    # Generate unique trace ID (timestamp + hash)
    trace_ts = time.strftime("%Y%m%d_%H%M%S")
    trace_id = f"{trace_ts}_{ck[:8]}"
    
    # Extract task type from prompt_file for easier filtering
    task_type = Path(prompt_file).stem if prompt_file else "unknown"
    
    # Create filename with case_id and task type
    trace_filename = f"{case_id}_{task_type}_{trace_ts}"
    
    # Save trace files
    # 1. Prompt file (rendered prompt with all variables substituted)
    prompt_path = trace_dir / f"{trace_filename}_prompt.txt"
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(f"# LLM Call Trace\n")
        f.write(f"# Paper ID: {paper_id}\n")
        f.write(f"# Case ID: {case_id}\n")
        f.write(f"# Timestamp: {trace_ts}\n")
        f.write(f"# Model: {model}\n")
        f.write(f"# Cache Key: {cache_key}\n")
        f.write(f"# Prompt File: {prompt_file}\n")
        f.write(f"# Elapsed Time: {elapsed_time:.2f}s\n")
        f.write(f"# Trace ID: {trace_id}\n")
        f.write("=" * 80 + "\n\n")
        f.write(rendered)
    
    # 2. Response file (raw LLM output)
    response_path = trace_dir / f"{trace_filename}_response.txt"
    with open(response_path, "w", encoding="utf-8") as f:
        f.write(f"# LLM Response\n")
        f.write(f"# Paper ID: {paper_id}\n")
        f.write(f"# Case ID: {case_id}\n")
        f.write(f"# Trace ID: {trace_id}\n")
        f.write(f"# Model: {model}\n")
        f.write(f"# Elapsed Time: {elapsed_time:.2f}s\n")
        f.write("=" * 80 + "\n\n")
        f.write(raw)
    
    # 3. Metadata JSON (structured for programmatic access)
    meta_path = trace_dir / f"{trace_filename}_meta.json"
    trace_meta = {
        "trace_id": trace_id,
        "paper_id": paper_id,
        "case_id": case_id,
        "timestamp": trace_ts,
        "model": model,
        "prompt_file": prompt_file,
        "task_type": task_type,
        "cache_key": cache_key,
        "elapsed_seconds": round(elapsed_time, 2),
        "prompt_length": len(rendered),
        "response_length": len(raw),
        "thinking_enabled": thinking,
        "cache_hash": ck,
    }
    write_json(meta_path, trace_meta)
    
    logger.debug(f"LLM trace saved: {paper_id}/{trace_filename} ({elapsed_time:.2f}s)")
    # =========================================================================
    # END LLM TRACE LOGGING
    # =========================================================================

    # Parse JSON with fallback
    obj = None
    try:
        obj = _json_parse_strict(raw)
    except Exception:
        # Last resort: extract JSON between first { and last }
        raw2 = raw.strip()
        i = raw2.find("{")
        j = raw2.rfind("}")
        if i != -1 and j != -1 and j > i:
            try:
                obj = json.loads(raw2[i:j+1])
            except:
                pass
        
        if obj is None:
            # Try to find a JSON array
            i = raw2.find("[")
            j = raw2.rfind("]")
            if i != -1 and j != -1 and j > i:
                try:
                    arr = json.loads(raw2[i:j+1])
                    obj = {"measurements": arr}
                except:
                    pass
        
        if obj is None:
            # Return empty dict instead of raising
            return {"error": f"Failed to parse JSON from LLM response", "raw_preview": raw[:300]}

    # Ensure obj is a dict
    if not isinstance(obj, dict):
        if isinstance(obj, list):
            obj = {"measurements": obj}
        else:
            obj = {"value": obj}

    # Post-processing: If response has "thinking" and "output", unwrap the "output"
    if isinstance(obj, dict) and "output" in obj and "thinking" in obj:
        # Save thinking to a separate cache file for debugging
        thinking_cache = cache_dir_p / f"{ck}_thinking.txt"
        with open(thinking_cache, "w", encoding="utf-8") as f:
            f.write(str(obj.get("thinking", "")))
        output = obj["output"]
        # Ensure output is dict
        if isinstance(output, dict):
            obj = output
        elif isinstance(output, list):
            obj = {"measurements": output}
        else:
            obj = {"value": output}

    # Cache and return
    write_json(cache_path, obj)
    return obj


# ============================================================================
# Quick Test
# ============================================================================
def test_connection():
    """Test LLM connection."""
    provider = LLMConfig.PROVIDER
    model = get_model_name("small")
    
    print(f"Testing connection to {provider} with model {model}...")
    
    try:
        response = call_provider(
            model=model,
            system_and_user_prompt='Return ONLY this JSON: {"status": "ok", "model": "' + model + '"}',
            thinking=False
        )
        print(f"Response: {response[:200]}")
        return True
    except Exception as e:
        print(f"Error: {e}")
        return False


if __name__ == "__main__":
    test_connection()
