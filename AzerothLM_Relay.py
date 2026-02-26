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
import logging
import threading
from dotenv import load_dotenv, set_key
from litellm import completion
from mcp.server.fastmcp import FastMCP
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
import pyfiglet

load_dotenv()
logger = logging.getLogger("azerothlm")
PATH = os.path.normpath(os.getenv("WOW_SAVED_VARIABLES_PATH") or "")
ADDON_PATH = os.path.normpath(os.getenv("WOW_ADDON_PATH") or "")
SIGNAL_PATH = os.path.join(ADDON_PATH, "AzerothLM_Signal.lua")

MODEL_NAME = os.getenv("MODEL_NAME", "gemini/gemini-2.5-flash")
TESTING_MODE = os.getenv("TESTING_MODE", "false").lower() == "true"


CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.json")
JOURNAL_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "journal_state.json")
COOLDOWN_TIMER = 10
LAST_CALL_TIME = 0
DEBUG_MODE = False
_debug_enabled = False  # Set True at startup when --debug or DEBUG=true
MAX_RESPONSE_CHARS = 2000
usage_stats = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_hits": 0}

_sv_cache = None  # Cached result of read_saved_variables_db()
_sv_mtime = 0     # os.path.getmtime(PATH) when _sv_cache was populated
_response_cache = None  # In-memory AI response cache (loaded from cache.json on first use)
_CACHE_MAX_ENTRIES = 100

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
    "You are a World of Warcraft: The Burning Crusade Classic advisor. "
    "The player's character data is provided as formatted text.\n\n"

    "ADVICE GUIDELINES:\n"
    "- Give specific, actionable advice using the character's actual gear, level, class, talents, and quests.\n"
    "- Consider the talent spec to determine role (tank, healer, DPS) and tailor recommendations.\n"
    "- When suggesting gear upgrades, name the item and its source (dungeon, quest, reputation vendor, crafted).\n"
    "- For professions, focus on primary crafting skills; secondary skills are lower priority.\n"
    "- Use reputation standings to identify unlocks the player is close to.\n\n"

    "ITEM FORMAT:\n"
    "- When mentioning a specific equippable item, write it EXACTLY as [Item Name] (ID:itemID).\n"
    "- Always include the numeric item ID from Wowhead so the display can link it.\n"
    "- Example: [Sunfury Bow of the Phoenix] (ID:28016)\n"
    "- Do NOT use this format for quests, NPCs, or zones — only equippable items.\n\n"

    "RESPONSE FORMAT:\n"
    "- Keep responses under 1500 characters.\n"
    "- Use short paragraphs and bullet points (use the bullet character).\n"
    "- Do NOT use markdown: no # headers, **bold**, *italic*, ``` code blocks, or tables.\n"
    "- Use plain text with line breaks only.\n"
    "- Start with the most actionable recommendation.\n"
    "- Do not ask what game the user is playing."
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
        except ValueError: return float(num_str)

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

# -----------------------------------------------------------------------------
# Question Classification & Context Formatting
# -----------------------------------------------------------------------------
_CONTEXT_KEYWORDS = {
    "gear": {
        "gear", "weapon", "armor", "helm", "head", "neck", "shoulder", "chest",
        "waist", "belt", "legs", "pants", "feet", "boots", "wrist", "bracers",
        "hands", "gloves", "ring", "trinket", "back", "cloak", "offhand", "shield",
        "ranged", "bow", "gun", "wand", "upgrade", "item", "equip", "slot", "bis",
        "best in slot", "ilvl", "item level", "enchant", "gem", "socket",
        "mainhand", "main-hand", "off-hand", "two-hand",
    },
    "professions": {
        "profession", "crafting", "craft", "make", "create", "herbalism", "mining",
        "skinning", "leatherworking", "blacksmithing", "tailoring", "engineering",
        "alchemy", "enchanting", "jewelcrafting", "fishing", "cooking", "first aid",
        "recipe", "pattern", "schematic", "transmute", "potion", "elixir", "flask",
        "cloth", "leather", "metal", "ore", "herb", "skill", "rank", "level up",
    },
    "quests": {
        "quest", "quests", "questline", "objective", "complete", "turn in", "turnin",
        "chain", "storyline", "npc", "kill", "collect", "gather", "escort",
        "nagrand", "hellfire", "zangarmarsh", "terokkar", "shadowmoon", "blade's edge",
        "netherstorm", "daily", "dungeon quest", "group quest", "elite quest",
    },
    "reputations": {
        "reputation", "rep", "faction", "exalted", "revered", "honored", "friendly",
        "grind", "tabard", "scryers", "aldor", "consortium", "cenarion", "expedition",
        "sha'tar", "shattrath", "violet eye", "keepers of time", "lower city",
        "thrallmar", "honor hold", "kurenai", "maghar", "sporeggar", "ogri'la",
        "skyguard", "netherwing", "steamwheedle", "ashtongue",
    },
}

def classify_question(question):
    """Return the set of context sections relevant to this question via keyword matching.
    Always includes 'player'. Falls back to all sections if nothing matches."""
    sections = {"player"}
    q_lower = question.lower()
    matched = False
    for section, keywords in _CONTEXT_KEYWORDS.items():
        if any(kw in q_lower for kw in keywords):
            sections.add(section)
            matched = True
    if not matched:
        sections.update(_CONTEXT_KEYWORDS.keys())
    return sections

def format_context(context_dict, sections):
    """Format character context as readable text for the given sections."""
    if not context_dict:
        return "(No character context available)"
    lines = []

    player = context_dict.get("player", {})
    if player and "player" in sections:
        level = player.get("level", "?")
        cls = player.get("class", "?")
        race = player.get("race", "?")
        talents = player.get("talents", {})
        if talents:
            talent_str = " / ".join(f"{tree} {pts}" for tree, pts in talents.items())
            header = f"Level {level} {race} {cls} ({talent_str})"
        else:
            header = f"Level {level} {race} {cls}"
        lines.append(header)

        zone = player.get("zone", "")
        subzone = player.get("subzone", "")
        loc = f"{subzone} - {zone}" if subzone and zone else (zone or subzone or "")
        gold_raw = player.get("gold")
        loc_gold = f"Zone: {loc}" if loc else ""
        if gold_raw is not None:
            try:
                g_total = int(gold_raw)
                g = g_total // 10000
                s = (g_total % 10000) // 100
                c = g_total % 100
                gold_str = f"{g}g {s}s {c}c" if g else f"{s}s {c}c" if s else f"{c}c"
            except (TypeError, ValueError):
                gold_str = str(gold_raw)
            loc_gold = f"{loc_gold} | Gold: {gold_str}" if loc_gold else f"Gold: {gold_str}"
        if loc_gold:
            lines.append(loc_gold)

    if "gear" in sections:
        gear = context_dict.get("gear", {})
        if gear:
            lines.append("\nGear:")
            for slot, item in gear.items():
                name = item.get("name", "?")
                item_id = item.get("itemId", "?")
                lines.append(f"  {slot}: {name} (ID:{item_id})")

    if "professions" in sections:
        profs = context_dict.get("professions", [])
        if profs:
            lines.append("\nProfessions:")
            for p in profs:
                name = p.get("name", "?")
                rank = p.get("rank", 0)
                max_rank = p.get("maxRank", 0)
                lines.append(f"  {name}: {rank}/{max_rank}")

    if "quests" in sections:
        quests = context_dict.get("quests", [])
        if quests:
            lines.append(f"\nActive Quests ({len(quests)}):")
            for q in quests[:15]:
                title = q.get("title", "?")
                qlevel = q.get("level", "?")
                done = " [Done]" if q.get("isComplete") else ""
                lines.append(f"  [{qlevel}] {title}{done}")

    if "reputations" in sections:
        reps = context_dict.get("reputations", [])
        if reps:
            lines.append("\nReputations:")
            for r in reps:
                faction = r.get("faction", "?")
                standing = r.get("standing", "?")
                lines.append(f"  {faction}: {standing}")

    return "\n".join(lines)

def get_cache_key(model, query, context, history_fingerprint=""):
    raw = f"{model}{query}{context}{history_fingerprint}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()

def load_cache():
    global _response_cache
    if _response_cache is not None:
        return _response_cache
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                _response_cache = json.load(f)
                return _response_cache
        except Exception:
            pass
    _response_cache = {}
    return _response_cache

def save_cache(key, response):
    cache = load_cache()
    cache[key] = response
    # FIFO eviction: keep only the most recent entries (dict preserves insertion order)
    if len(cache) > _CACHE_MAX_ENTRIES:
        excess = len(cache) - _CACHE_MAX_ENTRIES
        for old_key in list(cache.keys())[:excess]:
            del cache[old_key]
    atomic_write(CACHE_FILE, json.dumps(cache, indent=2))

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
# CLI Display Helpers
# -----------------------------------------------------------------------------
def debug_print(console, msg):
    """Print a debug message if DEBUG_MODE is enabled."""
    if DEBUG_MODE:
        console.print(f"[dim][DEBUG] {msg}[/dim]")

def mcp_log(msg):
    """Log diagnostic message via stdlib logger. No-op if --debug not passed."""
    if _debug_enabled:
        logger.debug(msg)

def interpolate_color(start_rgb, end_rgb, t):
    """Linearly interpolate between two (r, g, b) tuples. t in [0.0, 1.0]."""
    return tuple(int(s + (e - s) * t) for s, e in zip(start_rgb, end_rgb))

def render_gradient_header(text_str, start_color=(255, 0, 200), end_color=(0, 220, 255)):
    """Render bold block-character ASCII art with horizontal gradient and shadow depth."""
    try:
        ascii_art = pyfiglet.figlet_format(text_str, font="ansi_shadow")
    except Exception:
        ascii_art = pyfiglet.figlet_format(text_str)

    lines = ascii_art.rstrip("\n").split("\n")
    max_width = max((len(line) for line in lines), default=1)
    shadow_chars = frozenset("╔╗╚╝═║")

    result = Text()
    for i, line in enumerate(lines):
        for j, char in enumerate(line):
            if char == " ":
                result.append(char)
            else:
                t = j / max(max_width - 1, 1)
                r, g, b = interpolate_color(start_color, end_color, t)
                if char in shadow_chars:
                    r, g, b = int(r * 0.4), int(g * 0.4), int(b * 0.4)
                    result.append(char, style=f"#{r:02x}{g:02x}{b:02x}")
                else:
                    result.append(char, style=f"bold #{r:02x}{g:02x}{b:02x}")
        if i < len(lines) - 1:
            result.append("\n")

    return result

def _journal_watcher(console, stop_event):
    """Background thread: watch journal_state.json for external changes when debug is on."""
    last_mtime = 0
    last_state = load_journal_state()

    try:
        if os.path.exists(JOURNAL_STATE_FILE):
            last_mtime = os.path.getmtime(JOURNAL_STATE_FILE)
    except OSError:
        pass

    while not stop_event.is_set():
        if not DEBUG_MODE:
            stop_event.wait(2.0)
            continue

        try:
            if not os.path.exists(JOURNAL_STATE_FILE):
                stop_event.wait(2.0)
                continue

            current_mtime = os.path.getmtime(JOURNAL_STATE_FILE)
            if current_mtime == last_mtime:
                stop_event.wait(1.5)
                continue

            last_mtime = current_mtime
            stop_event.wait(0.2)

            new_state = load_journal_state()

            old_slugs = set(last_state.get("topics", {}).keys())
            new_slugs = set(new_state.get("topics", {}).keys())

            for slug in new_slugs - old_slugs:
                title = new_state["topics"][slug].get("title", slug)
                console.print(f"[dim][WATCH] Topic created: '{title}' ({slug})[/dim]")

            for slug in old_slugs - new_slugs:
                title = last_state["topics"][slug].get("title", slug)
                console.print(f"[dim][WATCH] Topic deleted: '{title}' ({slug})[/dim]")

            for slug in new_slugs & old_slugs:
                old_count = len(last_state["topics"][slug].get("entries", []))
                new_count = len(new_state["topics"][slug].get("entries", []))
                if new_count > old_count:
                    added = new_count - old_count
                    title = new_state["topics"][slug].get("title", slug)
                    console.print(f"[dim][WATCH] {added} new entry in '{title}' ({slug})[/dim]")

            last_state = new_state

        except Exception:
            pass

        stop_event.wait(1.5)

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

def get_keyed_providers():
    """Like get_configured_providers() but excludes keyless providers (e.g. Ollama)."""
    return {k: v for k, v in get_configured_providers().items() if v["key_env"] is not None}

def persist_env_value(key, value):
    env_path = get_env_path()
    set_key(env_path, key, value, quote_mode="never")
    os.environ[key] = value

def validate_or_prompt_paths(interactive):
    """Validate WOW_SAVED_VARIABLES_PATH and WOW_ADDON_PATH.
    MCP mode (interactive=False): hard exit on missing config.
    CLI mode (interactive=True): prompt user to enter paths interactively."""
    global PATH, ADDON_PATH, SIGNAL_PATH
    paths_ok = True

    if not PATH or PATH == "." or "YOUR_ACCOUNT_NAME" in PATH:
        if not interactive:
            print("Configuration Error: Please update WOW_SAVED_VARIABLES_PATH in your .env file")
            sys.exit(1)
        tmp = Console()
        tmp.print("\n[bold yellow]WOW_SAVED_VARIABLES_PATH is not configured.[/bold yellow]")
        tmp.print(
            "This should point to your AzerothLM SavedVariables file, e.g.:\n"
            "  [dim]C:\\Program Files (x86)\\World of Warcraft\\_anniversary_\\WTF\\"
            "Account\\YOURNAME\\SavedVariables\\AzerothLM.lua[/dim]\n"
        )
        try:
            val = tmp.input("[bold]Enter WOW_SAVED_VARIABLES_PATH:[/bold] ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nSetup cancelled.")
            sys.exit(1)
        if val:
            persist_env_value("WOW_SAVED_VARIABLES_PATH", val)
            PATH = os.path.normpath(val)
        else:
            paths_ok = False

    if not ADDON_PATH or ADDON_PATH == ".":
        if not interactive:
            print("Configuration Error: Please set WOW_ADDON_PATH in your .env file")
            sys.exit(1)
        tmp = Console()
        tmp.print("\n[bold yellow]WOW_ADDON_PATH is not configured.[/bold yellow]")
        tmp.print(
            "This should point to your AzerothLM addon directory, e.g.:\n"
            "  [dim]C:\\Program Files (x86)\\World of Warcraft\\_anniversary_\\"
            "Interface\\AddOns\\AzerothLM[/dim]\n"
        )
        try:
            val = tmp.input("[bold]Enter WOW_ADDON_PATH:[/bold] ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nSetup cancelled.")
            sys.exit(1)
        if val:
            persist_env_value("WOW_ADDON_PATH", val)
            ADDON_PATH = os.path.normpath(val)
            SIGNAL_PATH = os.path.join(ADDON_PATH, "AzerothLM_Signal.lua")
        else:
            paths_ok = False

    if not paths_ok:
        print("Configuration Error: Required paths not set. Please configure .env and restart.")
        sys.exit(1)

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
    atomic_write(JOURNAL_STATE_FILE, json.dumps(state, indent=2, ensure_ascii=False))

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

def atomic_write(path, content, encoding='utf-8'):
    """Write to a temp file then atomically rename to target path."""
    tmp_path = path + ".tmp"
    with open(tmp_path, 'w', encoding=encoding) as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)

# -----------------------------------------------------------------------------
# Context Reading
# -----------------------------------------------------------------------------
SLOT_NAMES = {
    1: "Head", 2: "Neck", 3: "Shoulder", 4: "Shirt", 5: "Chest",
    6: "Waist", 7: "Legs", 8: "Feet", 9: "Wrist", 10: "Hands",
    11: "Ring1", 12: "Ring2", 13: "Trinket1", 14: "Trinket2",
    15: "Back", 16: "MainHand", 17: "OffHand", 18: "Ranged", 19: "Tabard",
}

STANDING_NAMES = {
    1: "Hated", 2: "Hostile", 3: "Unfriendly", 4: "Neutral",
    5: "Friendly", 6: "Honored", 7: "Revered", 8: "Exalted",
}

def parse_item_link(raw_link):
    """Extract name and itemId from a WoW item link string."""
    if not raw_link:
        return None
    name_match = re.search(r'\[([^\]]+)\]', raw_link)
    if not name_match:
        return None
    id_match = re.search(r'item:(\d+)', raw_link)
    return {
        "name": name_match.group(1),
        "itemId": int(id_match.group(1)) if id_match else None,
    }

def read_saved_variables_db():
    """Read and parse the full AzerothLM_DB from SavedVariables.
    Result is cached by file mtime — only re-parsed when WoW writes a new version."""
    global _sv_cache, _sv_mtime
    mcp_log(f"read_sv: reading {PATH}")
    if not os.path.exists(PATH):
        mcp_log("read_sv: file not found")
        _sv_cache = None
        _sv_mtime = 0
        return None

    try:
        current_mtime = os.path.getmtime(PATH)
    except OSError:
        current_mtime = 0

    if _sv_cache is not None and current_mtime == _sv_mtime:
        mcp_log(f"read_sv: cache hit (mtime={current_mtime})")
        return _sv_cache

    wait_for_file_ready(PATH)
    try:
        with open(PATH, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        mcp_log(f"read_sv: read error — {e}")
        return None

    if "AzerothLM_DB" not in content:
        mcp_log("read_sv: AzerothLM_DB not in file")
        return None

    try:
        parser = LuaParser(content)
        result = parser.parse()
        _sv_cache = result
        _sv_mtime = current_mtime
        mcp_log(f"read_sv: parsed OK ({len(content)} chars)")
        return result
    except Exception as e:
        mcp_log(f"read_sv: parse error — {e}")
        return None

def read_game_context():
    db = read_saved_variables_db()
    if not db:
        return {}

    context = {}

    # Player basics
    context["player"] = {
        "level": db.get("level"),
        "class": db.get("class"),
        "race": db.get("race"),
        "zone": db.get("zone"),
        "subzone": db.get("subzone"),
        "gold": db.get("gold"),
    }

    # Talents
    talents = db.get("talents", {})
    if isinstance(talents, dict):
        decoded_talents = {}
        for k, t in talents.items():
            if isinstance(t, dict) and "name" in t:
                decoded_talents[decode_hex(t["name"])] = t.get("spent", 0)
        context["player"]["talents"] = decoded_talents

    # Gear — structured with slot names and parsed item links
    gear = db.get("gear", {})
    decoded_gear = {}
    if isinstance(gear, dict):
        for k, v in gear.items():
            slot_num = int(k)
            slot_name = SLOT_NAMES.get(slot_num, f"Slot{slot_num}")
            if v:
                raw = decode_hex(v)
                parsed = parse_item_link(raw)
                if parsed:
                    decoded_gear[slot_name] = parsed
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

    # Reputations
    reps = db.get("reputations", {})
    if isinstance(reps, dict):
        decoded_reps = []
        sorted_keys = sorted([k for k in reps.keys() if isinstance(k, int)])
        for k in sorted_keys:
            r = reps[k]
            if isinstance(r, dict):
                decoded_reps.append({
                    "faction": decode_hex(r.get("name", "")),
                    "standing": STANDING_NAMES.get(r.get("standing", 0), "Unknown"),
                })
        context["reputations"] = decoded_reps

    mcp_log(
        f"read_context: player={db.get('class')} lv{db.get('level')}, "
        f"gear slots={len(decoded_gear)}, quests={len(decoded_quests)}"
    )
    return context

# -----------------------------------------------------------------------------
# Pending Action Processing
# -----------------------------------------------------------------------------
def process_pending_actions():
    """Read pendingActions from SavedVariables, apply to journal_state.
    Returns (max_processed_timestamp, journal_state)."""
    db = read_saved_variables_db()
    state = load_journal_state()
    if not db:
        return 0, state

    pending = db.get("pendingActions", {})
    if not pending or not isinstance(pending, dict):
        mcp_log("pending: found 0 pending actions")
        mcp_log("pending: max_ts=0")
        return 0, state

    # LuaParser returns positional arrays as {1: val, 2: val, ...}
    sorted_keys = sorted([k for k in pending.keys() if isinstance(k, int)])
    if not sorted_keys:
        mcp_log("pending: found 0 pending actions")
        mcp_log("pending: max_ts=0")
        return 0, state

    mcp_log(f"pending: found {len(sorted_keys)} pending actions")
    max_timestamp = 0

    for k in sorted_keys:
        action_data = pending[k]
        if not isinstance(action_data, dict):
            continue

        action = action_data.get("action", "")
        slug = action_data.get("slug", "")
        timestamp = action_data.get("timestamp", 0)
        mcp_log(f"pending: {action} slug='{slug}' ts={timestamp}")

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

    mcp_log(f"pending: max_ts={max_timestamp}")
    if max_timestamp > 0:
        save_journal_state(state)

    return max_timestamp, state

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
                        mcp_log(f"call_ai: retry {retries}/{max_retries} after {delay}s (429 rate limit)")
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

def testing_call_ai(user_query, context_dict, topic_title):
    responses = [
        "[TEST MODE] Analyzing your gear... You should prioritize upgrading your weapon in Karazhan.",
        "[TEST MODE] Based on your professions, you should focus on transmuting Primal Might.",
        "[TEST MODE] Your quest log indicates you are in Nagrand. Have you completed the Ring of Blood?",
        "[TEST MODE] Detected 306 Skinning... you should head to Nagrand to farm Clefthoof leather.",
        "[TEST MODE] Mock Response: The Legion holds no sway here.",
    ]
    time.sleep(0.5)
    return random.choice(responses)

def call_ai(user_query, context_dict, topic_title, history=None, console=None):
    global LAST_CALL_TIME

    if TESTING_MODE:
        mcp_log("call_ai: TESTING_MODE — returning mock response")
        if console:
            debug_print(console, "Testing mode — returning mock response")
        return testing_call_ai(user_query, context_dict, topic_title)

    # Classify question and format context for relevant sections only
    sections = classify_question(user_query)
    mcp_log(f"call_ai: sections={sorted(sections)}")
    context_text = format_context(context_dict, sections)
    if console:
        debug_print(console, f"Sections: {sorted(sections)}, context: {len(context_text)} chars")

    # Rate Limiting
    elapsed = time.time() - LAST_CALL_TIME
    if elapsed < COOLDOWN_TIMER:
        wait_time = COOLDOWN_TIMER - elapsed
        mcp_log(f"call_ai: rate limit — waiting {wait_time:.1f}s (cooldown={COOLDOWN_TIMER}s)")
        if console:
            debug_print(console, f"Rate limit: waiting {wait_time:.1f}s")
        time.sleep(wait_time)

    # History windowing — cap at last 10 entries, cache key uses original length
    full_history = history or []
    if len(full_history) > 10:
        mcp_log(f"call_ai: windowing history {len(full_history)} → 10 entries")
        windowed_history = full_history[-10:]
    else:
        windowed_history = full_history

    # Cache key includes history fingerprint so new entries cause a miss
    last_ts = full_history[-1].get("timestamp", 0) if full_history else 0
    history_fingerprint = f"{len(full_history)}:{last_ts}"
    cache_key = get_cache_key(MODEL_NAME, user_query, context_text, history_fingerprint)
    cache = load_cache()
    if cache_key in cache:
        usage_stats["cached_hits"] += 1
        mcp_log(f"call_ai: cache HIT {cache_key[:12]}...")
        if console:
            debug_print(console, f"Cache HIT — {cache_key[:12]}...")
        return cache[cache_key]

    mcp_log(f"call_ai: cache MISS — calling {MODEL_NAME}")
    if console:
        debug_print(console, f"Cache MISS — calling {MODEL_NAME}")

    # Build multi-turn messages
    messages = [{"role": "system", "content": SYSTEM_INSTRUCTION}]

    for entry in windowed_history:
        messages.append({"role": "user", "content": entry["question"]})
        messages.append({"role": "assistant", "content": entry.get("full_answer") or entry["answer"]})

    user_content = (
        f"Research topic: '{topic_title}'. "
        f"Prioritize information relevant to this topic while considering overall character data.\n\n"
        f"Character Context:\n{context_text}\n\n"
        f"Question: {user_query}"
    )
    messages.append({"role": "user", "content": user_content})

    mcp_log(f"call_ai: {len(messages)} messages, user content={len(user_content)} chars")
    if console:
        debug_print(console, f"Messages: {len(messages)}, content: {len(user_content)} chars")

    try:
        tokens_before = (usage_stats["prompt_tokens"], usage_stats["completion_tokens"])
        response_content = _execute_completion(messages)
        LAST_CALL_TIME = time.time()
        save_cache(cache_key, response_content)
        prompt_tokens = usage_stats["prompt_tokens"] - tokens_before[0]
        completion_tokens = usage_stats["completion_tokens"] - tokens_before[1]
        mcp_log(
            f"call_ai: response {len(response_content)} chars | "
            f"tokens: prompt={prompt_tokens} completion={completion_tokens}"
        )
        if console:
            debug_print(console, f"Response: {len(response_content)} chars")
        return response_content
    except Exception as e:
        mcp_log(f"call_ai: API ERROR — {e}")
        if console:
            debug_print(console, f"API error: {e}")
        return f"API Error: {str(e)}"

# -----------------------------------------------------------------------------
# Signal File Writing
# -----------------------------------------------------------------------------
def write_signal_file(state, ack_timestamp=None, console=None):
    topics = state.get("topics", {})
    mcp_log(f"signal: writing {len(topics)} topics to {SIGNAL_PATH}, ack={ack_timestamp}")
    if console:
        debug_print(console, f"Writing signal: {len(topics)} topics, ack={ack_timestamp}")

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
        try:
            atomic_write(SIGNAL_PATH, 'AzerothLM_Signal = {}\n')
            mcp_log("signal: write OK (empty)")
        except Exception as e:
            mcp_log(f"signal: write FAILED — {e}")
        return

    try:
        lua_table = to_lua(signal_data)
    except Exception as e:
        mcp_log(f"signal: serialization FAILED — {e}")
        raise RuntimeError(f"Lua serialization failed: {e}")

    try:
        atomic_write(SIGNAL_PATH, f'AzerothLM_Signal = {lua_table}\n')
        mcp_log("signal: write OK")
    except Exception as e:
        mcp_log(f"signal: write FAILED — {e}")
        raise

def sync_pending_and_write_signal(console=None, force_write=False):
    """Process any pending in-game actions, then rewrite the signal file with ack.
    Only writes signal file if pending actions were found or force_write is True.
    Returns (max_ts, state) so callers can reuse the loaded state."""
    mcp_log("sync: start")
    max_ts, state = process_pending_actions()
    mcp_log(f"sync: pending done, max_ts={max_ts}")
    if console and max_ts > 0:
        debug_print(console, f"Processed pending actions up to ts={max_ts}")
    if max_ts > 0 or force_write:
        write_signal_file(state, ack_timestamp=max_ts if max_ts > 0 else None, console=console)
    else:
        mcp_log("sync: skipped signal write (no pending actions)")
    mcp_log("sync: complete")
    return max_ts, state

# -----------------------------------------------------------------------------
# MCP Server
# -----------------------------------------------------------------------------
mcp = FastMCP("AzerothLM Research Relay")

@mcp.tool()
def create_topic(title: str) -> str:
    """Create a new research topic for the WoW journal. Returns the topic slug."""
    mcp_log(f"[TOOL] create_topic | title='{title}'")
    _, state = sync_pending_and_write_signal()
    slug = slugify(title)
    if not slug:
        mcp_log(f"[TOOL] create_topic | ERROR: invalid slug from title='{title}'")
        return "Error: Could not generate a valid slug from the title."
    if slug in state["topics"]:
        mcp_log(f"[TOOL] create_topic | DUPLICATE slug='{slug}'")
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
    mcp_log(f"[TOOL] create_topic | OK | slug='{slug}'")
    return f"Created topic '{title}' (slug: {slug})"

@mcp.tool()
def ask_question(topic_slug: str, question: str) -> str:
    """Ask a question on a research topic. Reads character context from WoW SavedVariables,
    calls the AI with full topic history for multi-turn context, auto-delivers to signal file."""
    mcp_log(f"[TOOL] ask_question | slug='{topic_slug}' | q='{question[:100]}'")
    _, state = sync_pending_and_write_signal()

    if topic_slug not in state["topics"]:
        mcp_log(f"[TOOL] ask_question | ERROR: topic '{topic_slug}' not found")
        if state["topics"]:
            return f"Error: Topic '{topic_slug}' not found. Use list_topics to see available slugs."
        else:
            return f"Error: No topics exist yet. Use create_topic to create one first."

    topic = state["topics"][topic_slug]

    # Read fresh character context
    context = read_game_context()

    # Call AI with conversation history
    history = topic.get("entries", [])
    mcp_log(f"[TOOL] ask_question | context={list(context.keys())} | history={len(history)} entries")
    ai_response = call_ai(question, context, topic["title"], history)

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
        mcp_log(f"[TOOL] ask_question | signal write FAILED — {e}")
        return f"AI responded but signal write failed: {e}\n\nResponse:\n{display_response}"

    mcp_log(f"[TOOL] ask_question | response={len(ai_response)} chars | truncated={ai_response != display_response}")
    return display_response

@mcp.tool()
def list_topics() -> str:
    """List all research topics with metadata."""
    mcp_log("[TOOL] list_topics")
    _, state = sync_pending_and_write_signal()
    topics = state.get("topics", {})

    if not topics:
        mcp_log("[TOOL] list_topics | 0 topics")
        return "No research topics yet. Use create_topic to start."

    mcp_log(f"[TOOL] list_topics | {len(topics)} topics")
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
    mcp_log(f"[TOOL] get_topic | slug='{topic_slug}'")
    _, state = sync_pending_and_write_signal()

    if topic_slug not in state["topics"]:
        mcp_log(f"[TOOL] get_topic | ERROR: topic '{topic_slug}' not found")
        return f"Error: Topic '{topic_slug}' not found."

    topic = state["topics"][topic_slug]
    mcp_log(f"[TOOL] get_topic | {len(topic.get('entries', []))} entries returned")
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
    mcp_log("[TOOL] get_character_context")
    sync_pending_and_write_signal()  # return value not needed — context comes from SV
    context = read_game_context()
    if not context:
        mcp_log("[TOOL] get_character_context | no context available")
        return "No character context available. The SavedVariables file may not exist yet (requires at least one /reload or logout in-game)."
    result = json.dumps(context, indent=2, ensure_ascii=False)
    mcp_log(f"[TOOL] get_character_context | {len(result)} chars")
    return result

@mcp.tool()
def delete_topic(topic_slug: str) -> str:
    """Delete a research topic from the journal."""
    mcp_log(f"[TOOL] delete_topic | slug='{topic_slug}'")
    _, state = sync_pending_and_write_signal()

    if topic_slug not in state["topics"]:
        mcp_log(f"[TOOL] delete_topic | ERROR: topic '{topic_slug}' not found")
        return f"Error: Topic '{topic_slug}' not found."

    title = state["topics"][topic_slug]["title"]
    del state["topics"][topic_slug]
    save_journal_state(state)

    # Rewrite signal file without the deleted topic
    try:
        if state["topics"]:
            write_signal_file(state)
        elif os.path.exists(SIGNAL_PATH):
            atomic_write(SIGNAL_PATH, 'AzerothLM_Signal = nil\n')
    except Exception:
        pass

    mcp_log(f"[TOOL] delete_topic | OK | '{title}' removed")
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
# Help Registry
# -----------------------------------------------------------------------------
COMMAND_HELP = {
    "new": {
        "usage": "/new <title>",
        "brief": "Create a new research topic",
        "category": "Topics",
        "detail": (
            "Creates a new topic to organize AI research around a theme.\n\n"
            "• [cyan]title[/cyan] — any descriptive phrase (e.g. 'Karazhan Gear' or 'Herbalism Route')\n"
            "• The title is converted to a slug: lowercase, spaces → hyphens\n"
            "  Example: 'Karazhan Gear' → slug [cyan]karazhan-gear[/cyan]\n"
            "• Use the slug with [cyan]/ask[/cyan], [cyan]/view[/cyan], and [cyan]/delete[/cyan]\n\n"
            "Example: [cyan]/new Karazhan Gear Upgrades[/cyan]"
        ),
    },
    "ask": {
        "usage": "/ask <slug> <question>",
        "brief": "Ask a question on a topic (AI + character context)",
        "category": "Topics",
        "detail": (
            "Asks the AI a question tied to the given topic slug.\n\n"
            "• [cyan]slug[/cyan] — the short ID for a topic (shown by [cyan]/topics[/cyan])\n"
            "• Conversation history is included for multi-turn context (last 10 entries)\n"
            "• Character context is filtered to relevant sections based on your question:\n"
            "  - Gear keywords (weapon, helm, slot, bis, ...) → gear data included\n"
            "  - Profession keywords (alchemy, crafting, recipe, ...) → profession data\n"
            "  - Quest keywords (questline, chain, objective, ...) → quest data\n"
            "  - Reputation keywords (faction, exalted, rep grind, ...) → rep data\n"
            "  - Player info (level, class, talents, zone) is always included\n"
            "  - If no keywords match, all data is sent as a fallback\n\n"
            "Example: [cyan]/ask karazhan-gear What trinkets should I prioritize?[/cyan]"
        ),
    },
    "topics": {
        "usage": "/topics",
        "brief": "List all research topics",
        "category": "Topics",
        "detail": None,
    },
    "view": {
        "usage": "/view <slug>",
        "brief": "View full Q&A history for a topic",
        "category": "Topics",
        "detail": None,
    },
    "delete": {
        "usage": "/delete <slug>",
        "brief": "Delete a topic and all its entries",
        "category": "Topics",
        "detail": None,
    },
    "model": {
        "usage": "/model [list|add|switch]",
        "brief": "Manage AI providers and models",
        "category": "Model & Config",
        "detail": (
            "[bold]Subcommands:[/bold]\n\n"
            "  [cyan]/model[/cyan] or [cyan]/model list[/cyan]\n"
            "    Show all providers with configuration status and active model.\n\n"
            "  [cyan]/model add[/cyan]\n"
            "    Interactively add an API key for a new provider. Opens a numbered\n"
            "    menu of unconfigured providers. Paste your key — saved to [cyan].env[/cyan]\n"
            "    and takes effect immediately (no restart needed).\n\n"
            "  [cyan]/model switch[/cyan]\n"
            "    Two-step menu: choose a provider, then choose a model within it.\n"
            "    Only shows providers with a key configured.\n"
            "    Selection is saved to [cyan].env[/cyan] and persists across sessions.\n\n"
            "[bold]Supported providers:[/bold] Google Gemini, OpenAI, Anthropic, Ollama (local/keyless)\n\n"
            "[bold]Notes:[/bold]\n"
            "  • Ollama requires a locally running server — no API key needed\n"
            "  • MODEL_NAME in .env uses provider/model format, e.g. [dim]gemini/gemini-2.5-flash[/dim]"
        ),
    },
    "test": {
        "usage": "/test [on|off]",
        "brief": "Toggle testing mode (mock AI responses)",
        "category": "Model & Config",
        "detail": (
            "Testing mode replaces live AI calls with fast mock responses.\n\n"
            "  [cyan]/test on[/cyan]  — enable testing mode, run config verification check\n"
            "  [cyan]/test off[/cyan] — disable testing mode, use live model\n"
            "  [cyan]/test[/cyan]     — show current status\n\n"
            "When active, responses are prefixed [bold yellow][TEST MODE][/bold yellow] "
            "and no API tokens are consumed.\n"
            "Testing mode is persisted to [cyan].env[/cyan] as TESTING_MODE=true.\n\n"
            "Running [cyan]/test on[/cyan] also triggers a configuration check that\n"
            "validates your .env, API keys, file paths, and addon directory."
        ),
    },
    "context": {
        "usage": "/context",
        "brief": "Show character context (gear, professions, quests, reputations)",
        "category": "Info",
        "detail": (
            "Displays current character data read from WoW SavedVariables.\n\n"
            "Data includes: level, class, race, talent spec, gear slots, professions,\n"
            "active quests, and faction reputations.\n\n"
            "If no data appears:\n"
            "  1. Log in to your character in-game\n"
            "  2. Run [cyan]/alm scan[/cyan] in-game to capture current data\n"
            "  3. Type [cyan]/reload[/cyan] or log out to save the SavedVariables file\n\n"
            "The relay reads this file on every [cyan]/ask[/cyan] call.\n"
            "Run [cyan]/alm scan[/cyan] in-game when your character changes significantly."
        ),
    },
    "usage": {
        "usage": "/usage",
        "brief": "Show API usage stats for this session",
        "category": "Info",
        "detail": None,
    },
    "status": {
        "usage": "/status",
        "brief": "Show relay configuration and file paths",
        "category": "Info",
        "detail": None,
    },
    "quit": {
        "usage": "/quit",
        "brief": "Exit the relay (also: /exit, /q)",
        "category": "Info",
        "detail": None,
    },
}

# -----------------------------------------------------------------------------
# Interactive CLI
# -----------------------------------------------------------------------------
def run_cli():
    console = Console()

    # Start journal watcher for MCP activity debug output
    watcher_stop = threading.Event()
    watcher_thread = threading.Thread(
        target=_journal_watcher,
        args=(console, watcher_stop),
        daemon=True,
    )
    watcher_thread.start()

    # ASCII art header with gradient
    header = render_gradient_header("AZEROTHLM")
    console.print(header)

    # Config info
    configured = get_configured_providers()
    keyed_providers = get_keyed_providers()
    active_provider = MODEL_NAME.split("/")[0] if "/" in MODEL_NAME else ""
    config_table = Table(show_header=False, box=None, padding=(0, 2))
    config_table.add_column(style="bold cyan", width=18)
    config_table.add_column()
    config_table.add_row("Model", f"[bold]{MODEL_NAME}[/bold]")
    if keyed_providers:
        provider_names = ", ".join(info["display"] for info in keyed_providers.values())
        config_table.add_row("Providers", f"[green]{provider_names}[/green]")
    elif "ollama" in configured:
        config_table.add_row("Providers", "[dim]Ollama (local, keyless)[/dim]")
    else:
        config_table.add_row("Providers", "[red]None configured![/red]")
    config_table.add_row("SavedVariables", f"[dim]{PATH}[/dim]")
    config_table.add_row("Addon Path", f"[dim]{ADDON_PATH}[/dim]")
    if TESTING_MODE:
        config_table.add_row("Testing Mode", "[bold yellow]ON[/bold yellow]")
    console.print(config_table)
    console.print()

    active_needs_key = PROVIDERS.get(active_provider, {}).get("key_env") is not None
    if not keyed_providers and active_needs_key:
        console.print(Panel(
            "[bold yellow]No API providers configured.[/bold yellow]\n\n"
            "To get started, either:\n"
            "  1. Run [cyan]/model add[/cyan] to set up a provider interactively\n"
            "  2. Edit your [cyan].env[/cyan] file directly and add an API key\n\n"
            "Supported providers: " + ", ".join(
                p["display"] for p in PROVIDERS.values() if p["key_env"]
            ),
            title="Setup Required",
            border_style="yellow",
        ))
    elif active_provider and active_provider not in keyed_providers and active_needs_key:
        console.print(
            f"[yellow]Warning: Active model '{MODEL_NAME}' uses provider '{active_provider}' "
            f"which has no API key configured. Use /model switch to change.[/yellow]\n"
        )

    console.print("[dim]Type /help for commands. Type /quit to exit.[/dim]\n")

    # Sync pending actions on startup
    with console.status("[dim]Syncing journal state...[/dim]", spinner="dots"):
        try:
            sync_pending_and_write_signal(console=console)
        except Exception:
            pass

    while True:
        try:
            raw = console.input("[bold magenta]>[/bold magenta] ").strip()
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
            if not rest.strip():
                # Overview table grouped by category
                help_table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
                help_table.add_column("Command", style="cyan")
                help_table.add_column("Description")
                for cat in ("Topics", "Model & Config", "Info"):
                    help_table.add_row(f"[bold dim]{cat}[/bold dim]", "")
                    for name, info in COMMAND_HELP.items():
                        if info["category"] == cat:
                            help_table.add_row(f"  {info['usage']}", info["brief"])
                console.print(help_table)
                console.print("\n[dim]Type /help <command> for details. Example: /help model[/dim]")
            else:
                subcmd = rest.strip().lower().split()[0].lstrip("/")
                info = COMMAND_HELP.get(subcmd)
                if info and info.get("detail"):
                    console.print(Panel(
                        info["detail"],
                        title=f"[bold cyan]{info['usage']}[/bold cyan]",
                        subtitle=info["brief"],
                        border_style="cyan",
                    ))
                elif info:
                    console.print(f"[cyan]{info['usage']}[/cyan] — {info['brief']}")
                else:
                    console.print(f"[yellow]No help entry for '{subcmd}'. Type /help for a list of commands.[/yellow]")

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
                write_signal_file(state, console=console)
            except Exception:
                pass
            debug_print(console, f"Created topic slug='{slug}'")
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

            _, state = sync_pending_and_write_signal(console=console)
            if slug not in state["topics"]:
                console.print(topic_not_found_hint(slug, state))
                continue

            topic = state["topics"][slug]

            with console.status(
                f"[bold cyan]Thinking about '{topic['title']}'...[/bold cyan]",
                spinner="dots",
            ):
                debug_print(console, f"Reading context from {PATH}")
                context = read_game_context()
                debug_print(console, f"Context keys: {list(context.keys())}")

                history = topic.get("entries", [])
                debug_print(console, f"History: {len(history)} entries")
                ai_response = call_ai(question, context, topic["title"], history, console=console)
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
                write_signal_file(state, console=console)
            except Exception as e:
                console.print(f"[red]Signal write failed: {e}[/red]")

            console.print(Panel(display_response, title=f"[bold]Q: {question}[/bold]", subtitle=f"[dim]{MODEL_NAME}[/dim]", border_style="green"))
            console.print("[dim]Signal file updated. /reload in-game to view.[/dim]")

        # -- /topics ------------------------------------------------------
        elif cmd == "/topics":
            _, state = sync_pending_and_write_signal(console=console)
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
                    write_signal_file(state, console=console)
                elif os.path.exists(SIGNAL_PATH):
                    atomic_write(SIGNAL_PATH, 'AzerothLM_Signal = {}\n')
            except Exception:
                pass
            debug_print(console, f"Deleted topic slug='{slug}'")
            console.print(f"[green]Deleted topic:[/green] {title} [dim](slug: {slug})[/dim]")

        # -- /context -----------------------------------------------------
        elif cmd == "/context":
            with console.status("[dim]Reading character data...[/dim]", spinner="dots"):
                context = read_game_context()
            if not context:
                console.print("[yellow]No character context available. Log in and /reload in-game first.[/yellow]")
                continue
            debug_print(console, f"Context loaded: {len(json.dumps(context))} chars")
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

    # Clean shutdown of watcher thread
    watcher_stop.set()
    watcher_thread.join(timeout=2.0)

# -----------------------------------------------------------------------------
# Entry Point
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AzerothLM Research Relay")
    parser.add_argument("--mcp", action="store_true", help="Run as MCP server (for Claude Code)")
    parser.add_argument("--debug", action="store_true", help="Enable verbose diagnostic output to stderr")
    args = parser.parse_args()

    # Validate required paths — interactive prompts in CLI mode, hard exit in MCP mode
    validate_or_prompt_paths(interactive=not args.mcp)

    debug_active = args.debug or os.getenv("DEBUG", "").lower() == "true"

    if debug_active:
        _debug_enabled = True
        _dbg_handler = logging.StreamHandler(sys.stderr)
        _dbg_handler.setFormatter(logging.Formatter("%(asctime)s [DBG] %(message)s", datefmt="%H:%M:%S"))
        logger.setLevel(logging.DEBUG)
        logger.addHandler(_dbg_handler)
        logger.propagate = False
        configured_providers = [k for k, v in PROVIDERS.items()
                                 if v["key_env"] is None or
                                 (os.getenv(v["key_env"], "") and
                                  not PLACEHOLDER_PATTERN.match(os.getenv(v["key_env"], "")))]
        logger.debug(
            f"Debug enabled | mode={'mcp' if args.mcp else 'cli'} | "
            f"model={MODEL_NAME} | testing={TESTING_MODE} | "
            f"providers={','.join(configured_providers) or 'none'}"
        )

    if args.mcp:
        mcp.run()
    else:
        if debug_active:
            DEBUG_MODE = True
        run_cli()
