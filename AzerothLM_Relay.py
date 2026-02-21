import re
import os
import sys
import time
import random
import json
import hashlib
import functools
from dotenv import load_dotenv
from litellm import completion
from mcp.server.fastmcp import FastMCP

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
def read_game_context():
    if not os.path.exists(PATH):
        return {}

    wait_for_file_ready(PATH)
    try:
        with open(PATH, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception:
        return {}

    if "AzerothLM_DB" not in content:
        return {}

    try:
        parser = LuaParser(content)
        db = parser.parse()
    except Exception:
        return {}

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
    return response.choices[0].message.content or ""

def testing_call_ai(user_query, game_context, topic_title):
    responses = [
        "Analyzing your gear... You should prioritize upgrading your weapon in Karazhan.",
        "Based on your professions, you should focus on transmuting Primal Might.",
        "Your quest log indicates you are in Nagrand. Have you completed the Ring of Blood?",
        "Detected 306 Skinning... you should head to Nagrand to farm Clefthoof leather.",
        "Mock Response: The Legion holds no sway here.",
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
def write_signal_file(state):
    topics = state.get("topics", {})
    if not topics:
        return

    signal_data = {}
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

    try:
        lua_table = to_lua(signal_data)
    except Exception as e:
        raise RuntimeError(f"Lua serialization failed: {e}")

    wait_for_file_ready(SIGNAL_PATH)
    with open(SIGNAL_PATH, 'w', encoding='utf-8') as f:
        f.write(f'AzerothLM_Signal = {lua_table}\n')

# -----------------------------------------------------------------------------
# MCP Server
# -----------------------------------------------------------------------------
mcp = FastMCP("AzerothLM Research Relay")

@mcp.tool()
def create_topic(title: str) -> str:
    """Create a new research topic for the WoW journal. Returns the topic slug."""
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
    state = load_journal_state()

    if topic_slug not in state["topics"]:
        return f"Error: Topic '{topic_slug}' not found. Use create_topic first."

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
    context = read_game_context()
    if not context:
        return "No character context available. The SavedVariables file may not exist yet (requires at least one /reload or logout in-game)."
    return json.dumps(context, indent=2, ensure_ascii=False)

@mcp.tool()
def delete_topic(topic_slug: str) -> str:
    """Delete a research topic from the journal."""
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
# Entry Point
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run()
