import re
import os
import sys
import time
import random
import json
import hashlib
import shlex
import argparse
import functools
from dotenv import load_dotenv, set_key
from litellm import completion
from mcp.server.fastmcp import FastMCP
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

load_dotenv()
PATH = os.path.normpath(os.getenv("WOW_SAVED_VARIABLES_PATH") or "")
ADDON_PATH = os.path.normpath(os.getenv("WOW_ADDON_PATH") or "")
SIGNAL_PATH = os.path.join(ADDON_PATH, "AzerothLM_Signal.lua")

MODEL_NAME = os.getenv("MODEL_NAME", "gemini/gemini-2.5-flash")
TESTING_MODE = os.getenv("TESTING_MODE", "false").lower() == "true"

if not PATH or PATH == "." or "YOUR_ACCOUNT_NAME" in PATH:
    print("Configuration Error: Please update WOW_SAVED_VARIABLES_PATH in your .env file")
    sys.exit(1)

if not ADDON_PATH or ADDON_PATH == ".":
    print("Configuration Error: Please set WOW_ADDON_PATH in your .env file (e.g. Interface/AddOns/AzerothLM)")
    sys.exit(1)

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.json")
JOURNAL_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "journal_state.json")
COOLDOWN_TIMER = 10
LAST_CALL_TIME = 0
MAX_RESPONSE_CHARS = 2000
usage_stats = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_hits": 0}

PLACEHOLDER_PATTERN = re.compile(r"^YOUR_.*_HERE$|^$")
PROVIDERS = {
    "gemini": {
        "key_env": "GEMINI_API_KEY",
        "display": "Google Gemini",
        "key_url": "https://aistudio.google.com/",
        "models": ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash", "gemini-1.5-pro"],
    },
    "openai": {
        "key_env": "OPENAI_API_KEY",
        "display": "OpenAI",
        "key_url": "https://platform.openai.com/api-keys",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "o3-mini"],
    },
    "anthropic": {
        "key_env": "ANTHROPIC_API_KEY",
        "display": "Anthropic",
        "key_url": "https://console.anthropic.com/",
        "models": ["claude-sonnet-4-20250514", "claude-haiku-4-5-20251001"],
    },
    "ollama": {
        "key_env": None,
        "display": "Ollama (Local)",
        "key_url": "https://ollama.com/download",
        "models": ["llama3", "mistral", "codellama"],
    },
}

SYSTEM_INSTRUCTION = (
    "You are a specialized AI assistant for World of Warcraft: The Burning Crusade Classic. "
    "Use the provided JSON context (gear, professions, quests) to give specific, actionable advice. "
    "Do not ask what game the user is playing; assume it is always TBC Classic. "
    "Keep responses under 1500 characters. Use short paragraphs and bullet points for scannability. "
    "Format with simple line breaks. Do not use markdown headers, tables, or code blocks."
)

# -----------------------------------------------------------------------------
# Lua Parser & Serializer
# -----------------------------------------------------------------------------
class LuaParser:
    def __init__(self, data):
        self.data = data
        self.idx = 0
        self.length = len(data)

    def skip_whitespace(self):
        while self.idx < self.length:
            char = self.data[self.idx]
            if char.isspace():
                self.idx += 1
            elif char == '-' and self.idx + 1 < self.length and self.data[self.idx+1] == '-':
                self.idx += 2
                while self.idx < self.length and self.data[self.idx] != '\n':
                    self.idx += 1
            else:
                break

    def parse(self):
        match = re.search(r'AzerothLM_DB\s*=\s*', self.data)
        if match:
            self.idx = match.end()
        return self.parse_value()

    def parse_value(self):
        self.skip_whitespace()
        if self.idx >= self.length: return None
        char = self.data[self.idx]

        if char == '{': return self.parse_table()
        elif char == '"' or char == "'": return self.parse_string()
        elif char.isdigit() or char == '-': return self.parse_number()
        elif self.data.startswith("true", self.idx): self.idx += 4; return True
        elif self.data.startswith("false", self.idx): self.idx += 5; return False
        elif self.data.startswith("nil", self.idx): self.idx += 3; return None
        return None

    def parse_table(self):
        self.idx += 1
        obj = {}
        list_idx = 1
        while self.idx < self.length:
            self.skip_whitespace()
            if self.idx >= self.length or self.data[self.idx] == '}':
                self.idx += 1; break

            key = None
            if self.data[self.idx] == '[':
                self.idx += 1
                key = self.parse_value()
                self.skip_whitespace()
                if self.data[self.idx] == ']': self.idx += 1
                self.skip_whitespace()
                if self.data[self.idx] == '=': self.idx += 1
            elif self.data[self.idx].isalpha() or self.data[self.idx] == '_':
                save_idx = self.idx
                while self.idx < self.length and (self.data[self.idx].isalnum() or self.data[self.idx] == '_'):
                    self.idx += 1
                potential_key = self.data[save_idx:self.idx]
                self.skip_whitespace()
                if self.data[self.idx] == '=':
                    self.idx += 1
                    key = potential_key
                else:
                    self.idx = save_idx

            val = self.parse_value()
            self.skip_whitespace()
            if self.idx < self.length and (self.data[self.idx] == ',' or self.data[self.idx] == ';'):
                self.idx += 1

            if key is not None:
                obj[key] = val
            else:
                obj[list_idx] = val
                list_idx += 1
        return obj

    def parse_string(self):
        quote = self.data[self.idx]
        self.idx += 1
        start = self.idx
        while self.idx < self.length:
            if self.data[self.idx] == quote and self.data[self.idx-1] != '\\':
                break
            self.idx += 1
        val = self.data[start:self.idx]
        self.idx += 1
        return val.replace('\\"', '"').replace("\\'", "'").replace('\\n', '\n').replace('\\\\', '\\')

    def parse_number(self):
        start = self.idx
        if self.data[self.idx] == '-': self.idx += 1
        while self.idx < self.length and (self.data[self.idx].isdigit() or self.data[self.idx] == '.'):
            self.idx += 1
        num_str = self.data[start:self.idx]
        try: return int(num_str)
        except: return float(num_str)

def to_lua(obj, indent=0):
    spaces = "\t" * indent
    if isinstance(obj, list):
        if len(obj) == 0:
            return "{}"
        items = []
        for item in obj:
            items.append(f'{spaces}\t{to_lua(item, indent + 1)},')
        return "{\n" + "\n".join(items) + "\n" + spaces + "}"
    elif isinstance(obj, dict):
        keys = sorted(obj.keys())
        is_list = True
        if len(keys) > 0:
            if keys[0] != 1: is_list = False
            else:
                for i in range(len(keys)):
                    if keys[i] != i + 1:
                        is_list = False; break
        else:
            is_list = False

        items = []
        if is_list:
            for k in keys:
                items.append(f'{spaces}\t{to_lua(obj[k], indent + 1)},')
        else:
            for k, v in obj.items():
                key_str = f'["{k}"]' if isinstance(k, str) else f'[{k}]'
                items.append(f'{spaces}\t{key_str} = {to_lua(v, indent + 1)},')

        return "{\n" + "\n".join(items) + "\n" + spaces + "}"
    elif isinstance(obj, str):
        val = obj.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
        return f'"{val}"'
    elif isinstance(obj, bool):
        return "true" if obj else "false"
    elif obj is None:
        return "nil"
    return str(obj)

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def decode_hex(s):
    if isinstance(s, str):
        try:
            return bytes.fromhex(s).decode('utf-8', errors='ignore')
        except ValueError:
            return s
    return s

def slugify(title):
    slug = title.lower().strip()
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'[\s]+', '-', slug)
    slug = re.sub(r'-+', '-', slug)
    return slug.strip('-')

def topic_not_found_hint(slug, state):
    """Return a formatted error + hint for a missing topic slug."""
    if state["topics"]:
        return (f"[red]Topic '{slug}' not found.[/red]\n"
                f"[yellow]Hint: Use /topics to see available topics.[/yellow]")
    else:
        return (f"[red]Topic '{slug}' not found.[/red]\n"
                f"[yellow]Hint: No topics exist yet. Use /new <title> to create one.[/yellow]")

def get_cache_key(model, query, context):
    raw = f"{model}{query}{context}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_cache(key, response):
    cache = load_cache()
    cache[key] = response
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2)

def truncate_response(text):
    if len(text) <= MAX_RESPONSE_CHARS:
        return text
    truncated = text[:MAX_RESPONSE_CHARS]
    last_period = truncated.rfind('.')
    last_newline = truncated.rfind('\n')
    cut = max(last_period, last_newline)
    if cut > MAX_RESPONSE_CHARS // 2:
        truncated = truncated[:cut + 1]
    return truncated.rstrip() + "\n\n[Response trimmed for in-game display]"

# -----------------------------------------------------------------------------
# Provider Management
# -----------------------------------------------------------------------------
def get_env_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

def get_configured_providers():
    configured = {}
    for key, info in PROVIDERS.items():
        if info["key_env"] is None:
            configured[key] = info
            continue
        val = os.getenv(info["key_env"], "")
        if val and not PLACEHOLDER_PATTERN.match(val):
            configured[key] = info
    return configured

def persist_env_value(key, value):
    env_path = get_env_path()
    set_key(env_path, key, value, quote_mode="never")
    os.environ[key] = value

# -----------------------------------------------------------------------------
# Journal State Persistence
# -----------------------------------------------------------------------------
def load_journal_state():
    if os.path.exists(JOURNAL_STATE_FILE):
        try:
            with open(JOURNAL_STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {"topics": {}}

def save_journal_state(state):
    with open(JOURNAL_STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

# -----------------------------------------------------------------------------
# File Access
# -----------------------------------------------------------------------------
def wait_for_file_ready(path):
    retries = 0
    max_retries = 10
    while retries < max_retries:
        try:
            if os.path.exists(path):
                with open(path, 'a'):
                    pass
            return True
        except (IOError, PermissionError):
            retries += 1
            time.sleep(0.5)
    return False

# -----------------------------------------------------------------------------
# Context Reading
# -----------------------------------------------------------------------------
def read_saved_variables_db():
    """Read and parse the full AzerothLM_DB from SavedVariables."""
    if not os.path.exists(PATH):
        return None

    wait_for_file_ready(PATH)
    try:
        with open(PATH, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception:
        return None

    if "AzerothLM_DB" not in content:
        return None

    try:
        parser = LuaParser(content)
        return parser.parse()
    except Exception:
        return None

def read_game_context():
    db = read_saved_variables_db()
    if not db:
        return {}

    context = {}

    # Gear
    gear = db.get("gear", {})
    decoded_gear = {}
    if isinstance(gear, dict):
        for k, v in gear.items():
            if v:
                decoded_gear[str(k)] = decode_hex(v)
    context["gear"] = decoded_gear

    # Professions
    profs = db.get("professions", {})
    decoded_profs = []
    if isinstance(profs, dict):
        sorted_keys = sorted([k for k in profs.keys() if isinstance(k, int)])
        for k in sorted_keys:
            p = profs[k]
            if isinstance(p, dict):
                p_new = p.copy()
                if "name" in p_new:
                    p_new["name"] = decode_hex(p_new["name"])
                decoded_profs.append(p_new)
    context["professions"] = decoded_profs

    # Quests
    quests = db.get("quests", {})
    decoded_quests = []
    if isinstance(quests, dict):
        sorted_keys = sorted([k for k in quests.keys() if isinstance(k, int)])
        for k in sorted_keys:
            q = quests[k]
            if isinstance(q, dict):
                q_new = q.copy()
                if "title" in q_new:
                    q_new["title"] = decode_hex(q_new["title"])
                decoded_quests.append(q_new)
    context["quests"] = decoded_quests

    return context

# -----------------------------------------------------------------------------
# Pending Action Processing
# -----------------------------------------------------------------------------
def process_pending_actions():
    """Read pendingActions from SavedVariables, apply to journal_state. Returns max processed timestamp."""
    db = read_saved_variables_db()
    if not db:
        return 0

    pending = db.get("pendingActions", {})
    if not pending or not isinstance(pending, dict):
        return 0

    # LuaParser returns positional arrays as {1: val, 2: val, ...}
    sorted_keys = sorted([k for k in pending.keys() if isinstance(k, int)])
    if not sorted_keys:
        return 0

    state = load_journal_state()
    max_timestamp = 0

    for k in sorted_keys:
        action_data = pending[k]
        if not isinstance(action_data, dict):
            continue

        action = action_data.get("action", "")
        slug = action_data.get("slug", "")
        timestamp = action_data.get("timestamp", 0)

        if timestamp > max_timestamp:
            max_timestamp = timestamp

        if action == "delete_topic" and slug in state["topics"]:
            del state["topics"][slug]

        elif action == "clear_entries" and slug in state["topics"]:
            state["topics"][slug]["entries"] = []
            state["topics"][slug]["updated_at"] = timestamp

        elif action == "rename_topic" and slug in state["topics"]:
            new_title = action_data.get("newTitle", "")
            if new_title:
                state["topics"][slug]["title"] = new_title
                state["topics"][slug]["updated_at"] = timestamp

        elif action == "delete_entry" and slug in state["topics"]:
            entry_ts = action_data.get("entryTimestamp")
            if entry_ts:
                entries = state["topics"][slug].get("entries", [])
                state["topics"][slug]["entries"] = [
                    e for e in entries if e.get("timestamp") != entry_ts
                ]
                state["topics"][slug]["updated_at"] = timestamp

    if max_timestamp > 0:
        save_journal_state(state)

    return max_timestamp

# -----------------------------------------------------------------------------
# AI Calling
# -----------------------------------------------------------------------------
def retry_with_backoff(max_retries=3, base_delay=1):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            retries = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if "429" in str(e) and retries < max_retries:
                        retries += 1
                        delay = base_delay * (2 ** (retries - 1))
                        time.sleep(delay)
                    else:
                        raise e
        return wrapper
    return decorator

@retry_with_backoff(max_retries=3)
def _execute_completion(messages):
    response = completion(model=MODEL_NAME, messages=messages)
    usage_stats["calls"] += 1
    if hasattr(response, "usage") and response.usage:
        usage_stats["prompt_tokens"] += getattr(response.usage, "prompt_tokens", 0) or 0
        usage_stats["completion_tokens"] += getattr(response.usage, "completion_tokens", 0) or 0
    return response.choices[0].message.content or ""

def testing_call_ai(user_query, game_context, topic_title):
    responses = [
        "[TEST MODE] Analyzing your gear... You should prioritize upgrading your weapon in Karazhan.",
        "[TEST MODE] Based on your professions, you should focus on transmuting Primal Might.",
        "[TEST MODE] Your quest log indicates you are in Nagrand. Have you completed the Ring of Blood?",
        "[TEST MODE] Detected 306 Skinning... you should head to Nagrand to farm Clefthoof leather.",
        "[TEST MODE] Mock Response: The Legion holds no sway here.",
    ]
    time.sleep(0.5)
    return random.choice(responses)

def call_ai(user_query, game_context, topic_title, history=None):
    global LAST_CALL_TIME

    if TESTING_MODE:
        return testing_call_ai(user_query, game_context, topic_title)

    # Rate Limiting
    elapsed = time.time() - LAST_CALL_TIME
    if elapsed < COOLDOWN_TIMER:
        time.sleep(COOLDOWN_TIMER - elapsed)

    # Caching
    cache_key = get_cache_key(MODEL_NAME, user_query, game_context)
    cache = load_cache()
    if cache_key in cache:
        usage_stats["cached_hits"] += 1
        return cache[cache_key]

    # Build multi-turn messages
    messages = [{"role": "system", "content": SYSTEM_INSTRUCTION}]

    if history:
        for entry in history:
            messages.append({"role": "user", "content": entry["question"]})
            messages.append({"role": "assistant", "content": entry["answer"]})

    user_content = (
        f"Research topic: '{topic_title}'. "
        f"Prioritize information relevant to this topic while considering overall character data.\n\n"
        f"Character Context: {game_context}\n\n"
        f"Question: {user_query}"
    )
    messages.append({"role": "user", "content": user_content})

    try:
        response_content = _execute_completion(messages)
        LAST_CALL_TIME = time.time()
        save_cache(cache_key, response_content)
        return response_content
    except Exception as e:
        return f"API Error: {str(e)}"

# -----------------------------------------------------------------------------
# Signal File Writing
# -----------------------------------------------------------------------------
def write_signal_file(state, ack_timestamp=None):
    topics = state.get("topics", {})

    signal_data = {}

    if ack_timestamp:
        signal_data["_ack"] = {"processedUpTo": ack_timestamp}

    for slug, topic in topics.items():
        signal_data[slug] = {
            "title": topic["title"],
            "model": topic.get("model", MODEL_NAME),
            "createdAt": topic.get("created_at", 0),
            "updatedAt": topic.get("updated_at", 0),
            "entries": [
                {
                    "question": e["question"],
                    "answer": e["answer"],
                    "timestamp": e["timestamp"],
                }
                for e in topic.get("entries", [])
            ],
        }

    if not signal_data:
        wait_for_file_ready(SIGNAL_PATH)
        with open(SIGNAL_PATH, 'w', encoding='utf-8') as f:
            f.write('AzerothLM_Signal = nil\n')
        return

    try:
        lua_table = to_lua(signal_data)
    except Exception as e:
        raise RuntimeError(f"Lua serialization failed: {e}")

    wait_for_file_ready(SIGNAL_PATH)
    with open(SIGNAL_PATH, 'w', encoding='utf-8') as f:
        f.write(f'AzerothLM_Signal = {lua_table}\n')

def sync_pending_and_write_signal():
    """Process any pending in-game actions, then rewrite the signal file with ack."""
    max_ts = process_pending_actions()
    state = load_journal_state()
    write_signal_file(state, ack_timestamp=max_ts if max_ts > 0 else None)

# -----------------------------------------------------------------------------
# MCP Server
# -----------------------------------------------------------------------------
mcp = FastMCP("AzerothLM Research Relay")

@mcp.tool()
def create_topic(title: str) -> str:
    """Create a new research topic for the WoW journal. Returns the topic slug."""
    sync_pending_and_write_signal()
    slug = slugify(title)
    if not slug:
        return "Error: Could not generate a valid slug from the title."

    state = load_journal_state()
    if slug in state["topics"]:
        return f"Topic '{slug}' already exists. Use ask_question to add entries."

    now = int(time.time())
    state["topics"][slug] = {
        "title": title,
        "model": MODEL_NAME,
        "created_at": now,
        "updated_at": now,
        "entries": [],
    }
    save_journal_state(state)
    return f"Created topic '{title}' (slug: {slug})"

@mcp.tool()
def ask_question(topic_slug: str, question: str) -> str:
    """Ask a question on a research topic. Reads character context from WoW SavedVariables,
    calls the AI with full topic history for multi-turn context, auto-delivers to signal file."""
    sync_pending_and_write_signal()
    state = load_journal_state()

    if topic_slug not in state["topics"]:
        if state["topics"]:
            return f"Error: Topic '{topic_slug}' not found. Use list_topics to see available slugs."
        else:
            return f"Error: No topics exist yet. Use create_topic to create one first."

    topic = state["topics"][topic_slug]

    # Read fresh character context
    context = read_game_context()
    context_str = str(context)

    # Call AI with conversation history
    history = topic.get("entries", [])
    ai_response = call_ai(question, context_str, topic["title"], history)

    # Truncate for in-game display
    display_response = truncate_response(ai_response)

    # Store entry
    now = int(time.time())
    entry = {
        "question": question,
        "answer": display_response,
        "timestamp": now,
        "full_answer": ai_response if ai_response != display_response else None,
    }
    topic["entries"].append(entry)
    topic["updated_at"] = now
    topic["model"] = MODEL_NAME

    save_journal_state(state)

    # Auto-deliver to signal file
    try:
        write_signal_file(state)
    except Exception as e:
        return f"AI responded but signal write failed: {e}\n\nResponse:\n{display_response}"

    return display_response

@mcp.tool()
def list_topics() -> str:
    """List all research topics with metadata."""
    sync_pending_and_write_signal()
    state = load_journal_state()
    topics = state.get("topics", {})

    if not topics:
        return "No research topics yet. Use create_topic to start."

    lines = []
    for slug, topic in sorted(topics.items(), key=lambda x: x[1].get("updated_at", 0), reverse=True):
        entry_count = len(topic.get("entries", []))
        updated = time.strftime("%Y-%m-%d %H:%M", time.localtime(topic.get("updated_at", 0)))
        lines.append(
            f"- {topic['title']} (slug: {slug})\n"
            f"  Model: {topic.get('model', 'unknown')} | Entries: {entry_count} | Updated: {updated}"
        )
    return "\n".join(lines)

@mcp.tool()
def get_topic(topic_slug: str) -> str:
    """Get the full Q&A history for a research topic."""
    sync_pending_and_write_signal()
    state = load_journal_state()

    if topic_slug not in state["topics"]:
        return f"Error: Topic '{topic_slug}' not found."

    topic = state["topics"][topic_slug]
    lines = [
        f"# {topic['title']}",
        f"Model: {topic.get('model', 'unknown')}",
        f"Created: {time.strftime('%Y-%m-%d %H:%M', time.localtime(topic.get('created_at', 0)))}",
        f"Updated: {time.strftime('%Y-%m-%d %H:%M', time.localtime(topic.get('updated_at', 0)))}",
        "",
    ]

    for i, entry in enumerate(topic.get("entries", []), 1):
        ts = time.strftime("%H:%M", time.localtime(entry.get("timestamp", 0)))
        lines.append(f"## Q{i} [{ts}]: {entry['question']}")
        # Show full answer if available, otherwise the display answer
        answer = entry.get("full_answer") or entry["answer"]
        lines.append(answer)
        lines.append("")

    return "\n".join(lines)

@mcp.tool()
def get_character_context() -> str:
    """Read and decode current character context (gear, professions, quests) from WoW SavedVariables."""
    sync_pending_and_write_signal()
    context = read_game_context()
    if not context:
        return "No character context available. The SavedVariables file may not exist yet (requires at least one /reload or logout in-game)."
    return json.dumps(context, indent=2, ensure_ascii=False)

@mcp.tool()
def delete_topic(topic_slug: str) -> str:
    """Delete a research topic from the journal."""
    sync_pending_and_write_signal()
    state = load_journal_state()

    if topic_slug not in state["topics"]:
        return f"Error: Topic '{topic_slug}' not found."

    title = state["topics"][topic_slug]["title"]
    del state["topics"][topic_slug]
    save_journal_state(state)

    # Rewrite signal file without the deleted topic
    try:
        if state["topics"]:
            write_signal_file(state)
        elif os.path.exists(SIGNAL_PATH):
            with open(SIGNAL_PATH, 'w', encoding='utf-8') as f:
                f.write('AzerothLM_Signal = nil\n')
    except Exception:
        pass

    return f"Deleted topic '{title}' (slug: {topic_slug})"

# -----------------------------------------------------------------------------
# Interactive CLI — Command Handlers
# -----------------------------------------------------------------------------
def handle_model_list(console):
    configured = get_configured_providers()
    active_provider = MODEL_NAME.split("/")[0] if "/" in MODEL_NAME else ""
    active_model = MODEL_NAME.split("/", 1)[1] if "/" in MODEL_NAME else MODEL_NAME

    table = Table(title="AI Providers & Models", show_lines=True)
    table.add_column("Provider", style="bold")
    table.add_column("Status")
    table.add_column("Models")

    for pkey, pinfo in PROVIDERS.items():
        if pkey in configured:
            status = "[green]Ready[/green]"
        else:
            status = "[dim]Not configured[/dim]"

        model_strs = []
        for m in pinfo["models"]:
            if pkey == active_provider and m == active_model:
                model_strs.append(f"[bold cyan]{m} (active)[/bold cyan]")
            else:
                model_strs.append(m)
        table.add_row(pinfo["display"], status, ", ".join(model_strs))

    console.print(table)
    console.print(f"\n[dim]Active model: {MODEL_NAME}[/dim]")

def handle_model_add(console):
    configured = get_configured_providers()
    unconfigured = {
        k: v for k, v in PROVIDERS.items()
        if v["key_env"] is not None and k not in configured
    }

    if not unconfigured:
        console.print("[green]All providers are already configured![/green]")
        handle_model_list(console)
        return

    console.print("\n[bold]Add API Key[/bold]\n")
    options = list(unconfigured.items())
    for i, (pkey, pinfo) in enumerate(options, 1):
        console.print(f"  [cyan]{i}[/cyan]. {pinfo['display']}  [dim]({pinfo['key_url']})[/dim]")
    console.print(f"  [dim]0. Cancel[/dim]\n")

    try:
        choice = console.input("[bold]Select provider:[/bold] ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not choice or choice == "0":
        console.print("[dim]Cancelled.[/dim]")
        return

    try:
        idx = int(choice) - 1
        if idx < 0 or idx >= len(options):
            raise ValueError
    except ValueError:
        console.print("[red]Invalid selection.[/red]")
        return

    pkey, pinfo = options[idx]
    console.print(f"\nGet your API key from: [link={pinfo['key_url']}]{pinfo['key_url']}[/link]")

    try:
        api_key = console.input(f"[bold]Paste {pinfo['display']} API key:[/bold] ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not api_key:
        console.print("[dim]Cancelled (empty key).[/dim]")
        return

    persist_env_value(pinfo["key_env"], api_key)
    console.print(f"[green]Saved {pinfo['key_env']} to .env[/green]")
    console.print(f"[dim]Use /model switch to switch to a {pinfo['display']} model.[/dim]")

def handle_model_switch(console):
    global MODEL_NAME
    configured = get_configured_providers()

    if not configured:
        console.print("[yellow]No providers configured. Use /model add first.[/yellow]")
        return

    console.print("\n[bold]Switch Model[/bold]\n")
    options = list(configured.items())
    for i, (pkey, pinfo) in enumerate(options, 1):
        console.print(f"  [cyan]{i}[/cyan]. {pinfo['display']}")
    console.print(f"  [dim]0. Cancel[/dim]\n")

    try:
        choice = console.input("[bold]Select provider:[/bold] ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not choice or choice == "0":
        console.print("[dim]Cancelled.[/dim]")
        return

    try:
        idx = int(choice) - 1
        if idx < 0 or idx >= len(options):
            raise ValueError
    except ValueError:
        console.print("[red]Invalid selection.[/red]")
        return

    pkey, pinfo = options[idx]
    models = pinfo["models"]
    active_provider = MODEL_NAME.split("/")[0] if "/" in MODEL_NAME else ""
    active_model = MODEL_NAME.split("/", 1)[1] if "/" in MODEL_NAME else ""

    console.print(f"\n[bold]{pinfo['display']} Models[/bold]\n")
    for i, m in enumerate(models, 1):
        marker = " [cyan](active)[/cyan]" if pkey == active_provider and m == active_model else ""
        console.print(f"  [cyan]{i}[/cyan]. {m}{marker}")
    console.print(f"  [dim]0. Cancel[/dim]\n")

    try:
        choice = console.input("[bold]Select model:[/bold] ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not choice or choice == "0":
        console.print("[dim]Cancelled.[/dim]")
        return

    try:
        midx = int(choice) - 1
        if midx < 0 or midx >= len(models):
            raise ValueError
    except ValueError:
        console.print("[red]Invalid selection.[/red]")
        return

    new_model = f"{pkey}/{models[midx]}"
    MODEL_NAME = new_model
    persist_env_value("MODEL_NAME", new_model)
    console.print(f"\n[green]Switched to:[/green] [bold]{new_model}[/bold]")
    console.print("[dim]Choice saved to .env. Future sessions will use this model.[/dim]")

def run_config_check(console):
    configured = get_configured_providers()
    active_provider = MODEL_NAME.split("/")[0] if "/" in MODEL_NAME else ""

    checks = []

    # Check 1: .env exists and writable
    env_path = get_env_path()
    if os.path.exists(env_path):
        try:
            with open(env_path, 'a'):
                pass
            checks.append(("[green]PASS[/green]", f".env file exists and is writable"))
        except (IOError, PermissionError):
            checks.append(("[red]FAIL[/red]", f".env file exists but is not writable"))
    else:
        checks.append(("[red]FAIL[/red]", f".env file not found at {env_path}"))

    # Check 2: Active provider has key
    if active_provider in configured:
        checks.append(("[green]PASS[/green]", f"Active provider '{active_provider}' has API key configured"))
    elif active_provider:
        checks.append(("[red]FAIL[/red]", f"Active provider '{active_provider}' has no API key — /model add or /model switch"))
    else:
        checks.append(("[yellow]WARN[/yellow]", f"Could not determine provider from MODEL_NAME '{MODEL_NAME}'"))

    # Check 3: Configured providers count
    keyed = {k: v for k, v in configured.items() if v["key_env"] is not None}
    if keyed:
        names = ", ".join(v["display"] for v in keyed.values())
        checks.append(("[green]PASS[/green]", f"API keys configured: {names}"))
    else:
        checks.append(("[red]FAIL[/red]", "No API keys configured — use /model add"))

    # Check 4: SavedVariables path
    if os.path.exists(PATH):
        checks.append(("[green]PASS[/green]", "SavedVariables file found"))
    else:
        checks.append(("[yellow]WARN[/yellow]", "SavedVariables file not found (log in and /reload in-game)"))

    # Check 5: Addon path writable
    if os.path.isdir(ADDON_PATH):
        checks.append(("[green]PASS[/green]", "Addon path exists"))
    else:
        checks.append(("[red]FAIL[/red]", f"Addon path not found: {ADDON_PATH}"))

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold", width=8)
    table.add_column()
    for status, msg in checks:
        table.add_row(status, msg)
    console.print(Panel(table, title="Configuration Check", border_style="blue"))

def handle_test(console, rest):
    global TESTING_MODE
    subcmd = rest.strip().split()[0].lower() if rest.strip() else ""

    if subcmd == "on":
        TESTING_MODE = True
        persist_env_value("TESTING_MODE", "true")
        console.print("[yellow]Test mode enabled.[/yellow] API calls will return mock responses prefixed with [TEST MODE].")
        run_config_check(console)
    elif subcmd == "off":
        TESTING_MODE = False
        persist_env_value("TESTING_MODE", "false")
        console.print("[green]Test mode disabled.[/green] API calls will use the live model.")
    else:
        status = "[yellow]ON[/yellow]" if TESTING_MODE else "[green]off[/green]"
        console.print(f"Test mode: {status}")
        console.print("[dim]Usage: /test on | /test off[/dim]")

# -----------------------------------------------------------------------------
# Interactive CLI
# -----------------------------------------------------------------------------
def run_cli():
    console = Console()

    # Startup banner
    configured = get_configured_providers()
    config_table = Table(show_header=False, box=None, padding=(0, 2))
    config_table.add_column(style="bold cyan")
    config_table.add_column()
    config_table.add_row("Active Model", MODEL_NAME)
    if configured:
        provider_names = ", ".join(info["display"] for info in configured.values())
        config_table.add_row("Providers", provider_names)
    else:
        config_table.add_row("Providers", "[red]None configured![/red]")
    config_table.add_row("SavedVariables", PATH)
    config_table.add_row("Addon Path", ADDON_PATH)
    if TESTING_MODE:
        config_table.add_row("Testing Mode", "[bold yellow]ON[/bold yellow]")
    else:
        config_table.add_row("Testing Mode", "off")
    console.print(Panel(config_table, title="[bold]AzerothLM Research Relay[/bold]", border_style="green"))

    if not configured:
        console.print(Panel(
            "[bold yellow]No API providers configured.[/bold yellow]\n\n"
            "To get started, either:\n"
            "  1. Run [cyan]/model add[/cyan] to set up a provider interactively\n"
            "  2. Edit your [cyan].env[/cyan] file directly and add an API key\n\n"
            "Supported providers: " + ", ".join(p["display"] for p in PROVIDERS.values()),
            title="Setup Required",
            border_style="yellow",
        ))
    else:
        active_provider = MODEL_NAME.split("/")[0] if "/" in MODEL_NAME else ""
        if active_provider and active_provider not in configured:
            console.print(
                f"[yellow]Warning: Active model '{MODEL_NAME}' uses provider '{active_provider}' "
                f"which has no API key configured. Use /model switch to change.[/yellow]\n"
            )

    console.print("[dim]Type /help for commands. Type /quit to exit.[/dim]\n")

    # Sync pending actions on startup
    try:
        sync_pending_and_write_signal()
    except Exception:
        pass

    while True:
        try:
            raw = console.input("[bold cyan]>[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye.[/dim]")
            break

        if not raw:
            continue

        # Parse command and args
        if raw.startswith("/"):
            parts = raw.split(None, 1)
            cmd = parts[0].lower()
            rest = parts[1] if len(parts) > 1 else ""
        else:
            # Bare text without slash — treat as unknown
            console.print("[yellow]Unknown input. Type /help for commands.[/yellow]")
            continue

        # -- /help --------------------------------------------------------
        if cmd == "/help":
            help_table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
            help_table.add_column("Command", style="cyan")
            help_table.add_column("Description")
            help_table.add_row("/new <title>", "Create a new research topic (title becomes the slug)")
            help_table.add_row("/ask <slug> <question>", "Ask a question on a topic")
            help_table.add_row("/topics", "List all topics")
            help_table.add_row("/view <slug>", "View full Q&A history for a topic")
            help_table.add_row("/delete <slug>", "Delete a topic")
            help_table.add_row("/model", "Show providers and models")
            help_table.add_row("/model add", "Add a new provider API key")
            help_table.add_row("/model switch", "Switch to a different model")
            help_table.add_row("/test [on|off]", "Toggle test mode, or show current status")
            help_table.add_row("/context", "Show character context (gear, professions, quests)")
            help_table.add_row("/usage", "Show API usage stats for this session")
            help_table.add_row("/status", "Show relay configuration")
            help_table.add_row("/quit", "Exit the relay (also: /exit, /q)")
            console.print(help_table)

        # -- /quit --------------------------------------------------------
        elif cmd in ("/quit", "/exit", "/q"):
            console.print("[dim]Goodbye.[/dim]")
            break

        # -- /new <title> -------------------------------------------------
        elif cmd == "/new":
            if not rest:
                console.print("[yellow]Usage: /new <title>[/yellow]")
                continue
            title = rest.strip('"').strip("'")
            slug = slugify(title)
            if not slug:
                console.print("[red]Could not generate a valid slug from that title.[/red]")
                console.print("[yellow]Hint: Use letters, numbers, or spaces in the title.[/yellow]")
                continue
            state = load_journal_state()
            if slug in state["topics"]:
                existing_title = state["topics"][slug]["title"]
                console.print(f"[yellow]Topic '{slug}' already exists ('{existing_title}'). Choose a different title.[/yellow]")
                continue
            now = int(time.time())
            state["topics"][slug] = {
                "title": title, "model": MODEL_NAME,
                "created_at": now, "updated_at": now, "entries": [],
            }
            save_journal_state(state)
            try:
                write_signal_file(state)
            except Exception:
                pass
            console.print(f"[green]Created topic:[/green] {title} [dim](slug: {slug})[/dim]")

        # -- /ask <slug> <question> ---------------------------------------
        elif cmd == "/ask":
            try:
                tokens = shlex.split(rest)
            except ValueError:
                tokens = rest.split(None, 1)
            if len(tokens) < 2:
                console.print("[yellow]Usage: /ask <slug> <question>[/yellow]")
                continue
            slug = tokens[0]
            question = " ".join(tokens[1:])

            sync_pending_and_write_signal()
            state = load_journal_state()
            if slug not in state["topics"]:
                console.print(topic_not_found_hint(slug, state))
                continue

            topic = state["topics"][slug]
            console.print(f"[dim]Asking on '{topic['title']}'...[/dim]")

            context = read_game_context()
            context_str = str(context)
            history = topic.get("entries", [])
            ai_response = call_ai(question, context_str, topic["title"], history)
            display_response = truncate_response(ai_response)

            now = int(time.time())
            entry = {
                "question": question, "answer": display_response, "timestamp": now,
                "full_answer": ai_response if ai_response != display_response else None,
            }
            topic["entries"].append(entry)
            topic["updated_at"] = now
            topic["model"] = MODEL_NAME
            save_journal_state(state)

            try:
                write_signal_file(state)
            except Exception as e:
                console.print(f"[red]Signal write failed: {e}[/red]")

            console.print(Panel(display_response, title=f"[bold]Q: {question}[/bold]", border_style="green"))
            console.print("[dim]Signal file updated. /reload in-game to view.[/dim]")

        # -- /topics ------------------------------------------------------
        elif cmd == "/topics":
            sync_pending_and_write_signal()
            state = load_journal_state()
            topics = state.get("topics", {})
            if not topics:
                console.print("[yellow]No topics yet. Use /new to create one.[/yellow]")
                continue
            table = Table(title="Research Topics", show_lines=False)
            table.add_column("Slug", style="cyan")
            table.add_column("Title")
            table.add_column("Entries", justify="right")
            table.add_column("Model", style="dim")
            table.add_column("Updated", style="dim")
            for slug, t in sorted(topics.items(), key=lambda x: x[1].get("updated_at", 0), reverse=True):
                count = len(t.get("entries", []))
                updated = time.strftime("%Y-%m-%d %H:%M", time.localtime(t.get("updated_at", 0)))
                table.add_row(slug, t["title"], str(count), t.get("model", "?"), updated)
            console.print(table)

        # -- /view <slug> -------------------------------------------------
        elif cmd == "/view":
            slug = rest.strip().strip('"').strip("'")
            if not slug:
                console.print("[yellow]Usage: /view <slug>[/yellow]")
                continue
            state = load_journal_state()
            if slug not in state["topics"]:
                console.print(topic_not_found_hint(slug, state))
                continue
            topic = state["topics"][slug]
            console.print(f"\n[bold yellow]{topic['title']}[/bold yellow]")
            console.print(f"[dim]Model: {topic.get('model', '?')} | Created: {time.strftime('%Y-%m-%d %H:%M', time.localtime(topic.get('created_at', 0)))}[/dim]\n")
            entries = topic.get("entries", [])
            if not entries:
                console.print(f"[dim]No entries yet. Use /ask {slug} <question> to add one.[/dim]")
                continue
            for i, entry in enumerate(entries, 1):
                ts = time.strftime("%H:%M", time.localtime(entry.get("timestamp", 0)))
                console.print(f"[bold cyan]Q{i}[/bold cyan] [{ts}]: {entry['question']}")
                answer = entry.get("full_answer") or entry["answer"]
                console.print(f"[green]{answer}[/green]\n")

        # -- /delete <slug> -----------------------------------------------
        elif cmd == "/delete":
            slug = rest.strip().strip('"').strip("'")
            if not slug:
                console.print("[yellow]Usage: /delete <slug>[/yellow]")
                continue
            state = load_journal_state()
            if slug not in state["topics"]:
                console.print(topic_not_found_hint(slug, state))
                continue
            title = state["topics"][slug]["title"]
            del state["topics"][slug]
            save_journal_state(state)
            try:
                if state["topics"]:
                    write_signal_file(state)
                elif os.path.exists(SIGNAL_PATH):
                    with open(SIGNAL_PATH, 'w', encoding='utf-8') as f:
                        f.write('AzerothLM_Signal = nil\n')
            except Exception:
                pass
            console.print(f"[green]Deleted topic:[/green] {title} [dim](slug: {slug})[/dim]")

        # -- /context -----------------------------------------------------
        elif cmd == "/context":
            context = read_game_context()
            if not context:
                console.print("[yellow]No character context available. Log in and /reload in-game first.[/yellow]")
                continue
            console.print(Panel(json.dumps(context, indent=2, ensure_ascii=False), title="Character Context", border_style="blue"))

        # -- /usage -------------------------------------------------------
        elif cmd == "/usage":
            table = Table(title="API Usage (this session)", show_lines=False)
            table.add_column("Metric", style="cyan")
            table.add_column("Value", justify="right")
            table.add_row("Model", MODEL_NAME)
            table.add_row("API Calls", str(usage_stats["calls"]))
            table.add_row("Prompt Tokens", str(usage_stats["prompt_tokens"]))
            table.add_row("Completion Tokens", str(usage_stats["completion_tokens"]))
            total = usage_stats["prompt_tokens"] + usage_stats["completion_tokens"]
            table.add_row("Total Tokens", str(total))
            table.add_row("Cache Hits", str(usage_stats["cached_hits"]))
            console.print(table)

        # -- /status ------------------------------------------------------
        elif cmd == "/status":
            sv_exists = os.path.exists(PATH)
            signal_exists = os.path.exists(SIGNAL_PATH)
            configured = get_configured_providers()
            state = load_journal_state()
            topic_count = len(state.get("topics", {}))
            table = Table(show_header=False, box=None, padding=(0, 2))
            table.add_column(style="bold cyan")
            table.add_column()
            table.add_row("Model", MODEL_NAME)
            keyed = {k: v for k, v in configured.items() if v["key_env"] is not None}
            table.add_row("Providers", f"{len(keyed)} configured" if keyed else "[red]none[/red]")
            table.add_row("Testing Mode", "[bold yellow]ON[/bold yellow]" if TESTING_MODE else "off")
            table.add_row("SavedVariables", f"{'found' if sv_exists else 'NOT FOUND'}")
            table.add_row("Signal File", f"{'exists' if signal_exists else 'not yet created'}")
            table.add_row("Topics", str(topic_count))
            console.print(Panel(table, title="Relay Status", border_style="blue"))

        # -- /model -----------------------------------------------------------
        elif cmd == "/model":
            subcmd = rest.strip().split(None, 1)[0].lower() if rest.strip() else ""
            if subcmd in ("", "list"):
                handle_model_list(console)
            elif subcmd == "add":
                handle_model_add(console)
            elif subcmd == "switch":
                handle_model_switch(console)
            else:
                console.print("[yellow]Usage: /model [list|add|switch][/yellow]")

        # -- /test ------------------------------------------------------------
        elif cmd == "/test":
            handle_test(console, rest)

        else:
            console.print(f"[yellow]Unknown command: {cmd}. Type /help for commands.[/yellow]")

# -----------------------------------------------------------------------------
# Entry Point
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AzerothLM Research Relay")
    parser.add_argument("--mcp", action="store_true", help="Run as MCP server (for Claude Code)")
    args = parser.parse_args()
    if args.mcp:
        mcp.run()
    else:
        run_cli()
