import re
import os
import sys
import time
import random
import luadata
import json
import hashlib
import functools
from dotenv import load_dotenv
from litellm import completion
from filelock import FileLock, Timeout
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.text import Text
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn

load_dotenv()
MODEL_NAME = os.getenv("MODEL_NAME", "gemini/gemini-2.5-flash")
MOCK_MODE = os.getenv("MOCK_MODE", "false").lower() == "true"

# Path to your SavedVariables file
PATH = os.getenv("WOW_SAVED_VARIABLES_PATH")

if not PATH or "YOUR_ACCOUNT_NAME" in PATH:
    print("Configuration Error: Please update WOW_SAVED_VARIABLES_PATH in your .env file")
    sys.exit(1)

PATH = os.path.normpath(PATH)
LOCK_PATH = PATH + ".lock"
CACHE_FILE = "cache.json"
COOLDOWN_TIMER = 10
LAST_CALL_TIME = 0
last_processed_query = None

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
                # Skip comment
                self.idx += 2
                while self.idx < self.length and self.data[self.idx] != '\n':
                    self.idx += 1
            else:
                break

    def parse(self):
        # Extract the table part if it starts with variable assignment
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
        self.idx += 1 # {
        obj = {}
        list_idx = 1
        while self.idx < self.length:
            self.skip_whitespace()
            if self.idx >= self.length or self.data[self.idx] == '}':
                self.idx += 1; break
            
            key = None
            # Check for explicit key ["key"] or [1]
            if self.data[self.idx] == '[': 
                self.idx += 1
                key = self.parse_value()
                self.skip_whitespace()
                if self.data[self.idx] == ']': self.idx += 1
                self.skip_whitespace()
                if self.data[self.idx] == '=': self.idx += 1
            # Check for identifier key name =
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
                    # Not a key, backtrack (it's a value like true/false/nil or string start, though strings start with quote)
                    # Actually if it was alpha, it could be true/false/nil value, handled in parse_value
                    self.idx = save_idx
            
            val = self.parse_value()
            self.skip_whitespace()
            if self.idx < self.length and (self.data[self.idx] == ',' or self.data[self.idx] == ';'): 
                self.idx += 1
            
            if key is not None:
                obj[key] = val
            else:
                # Implicit index (list item)
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
        # Simple unescape
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
    if isinstance(obj, dict):
        # Check if it's a list (keys are 1..N)
        keys = sorted(obj.keys())
        is_list = True
        if len(keys) > 0:
            if keys[0] != 1: is_list = False
            else:
                for i in range(len(keys)):
                    if keys[i] != i + 1:
                        is_list = False; break
        else:
            is_list = False # Empty dict treated as empty table

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

def get_cache_key(model, query, context):
    raw = f"{model}{query}{context}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_cache(key, response):
    cache = load_cache()
    cache[key] = response
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2)

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
def _execute_completion(system_instruction, user_content):
    response = completion(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_content}
        ]
    )
    return response.choices[0].message.content or ""

def mock_call_ai(user_query, game_context, chat_name):
    responses = [
        "Analyzing your gear... You should prioritize upgrading your weapon in Karazhan.",
        "Based on your professions, you should focus on transmuting Primal Might.",
        "Your quest log indicates you are in Nagrand. Have you completed the Ring of Blood?",
        "Detected 306 Skinning... you should head to Nagrand to farm Clefthoof leather.",
        "Mock Response: The Legion holds no sway here.",
    ]
    time.sleep(2) # Simulate latency
    return random.choice(responses)

def call_ai(user_query, game_context, chat_name, live_console=None):
    global LAST_CALL_TIME
    
    if MOCK_MODE:
        return mock_call_ai(user_query, game_context, chat_name)

    # Rate Limiting
    elapsed = time.time() - LAST_CALL_TIME
    if elapsed < COOLDOWN_TIMER:
        time.sleep(COOLDOWN_TIMER - elapsed)

    # Caching
    cache_key = get_cache_key(MODEL_NAME, user_query, game_context)
    cache = load_cache()
    if cache_key in cache:
        if live_console:
            info_text = Text.from_markup(f"Chat: [bold cyan]{chat_name}[/]\nModel: [bold magenta]{MODEL_NAME}[/]\nQuery: {user_query}\n\n")
            info_text.append(Text("Using Cached Response", style="cyan"))
            live_console.update(get_dashboard(Panel(info_text, title="Cache Hit", border_style="cyan")))
            time.sleep(1.5)
        return cache[cache_key]

    # Universal system instruction for all models
    system_instruction = (
        "You are a specialized AI assistant for World of Warcraft: The Burning Crusade Classic. "
        "Use the provided JSON context (gear, professions, quests) to give specific, actionable advice. "
        "Do not ask what game the user is playing; assume it is always TBC Classic. "
        "Format your responses using simple line breaks for compatibility with the WoW UI."
    )

    user_content = f"The user is currently in a chat session titled '{chat_name}'. Prioritize information relevant to this topic while still considering their overall character data.\n\nContext: {game_context}\n\nQuestion: {user_query}"

    try:
        response_content = _execute_completion(system_instruction, user_content)
        LAST_CALL_TIME = time.time()
        save_cache(cache_key, response_content)
        return response_content
    except Exception as e:
        return f"API Error: {str(e)}"

print(f"AzerothLM Relay running on: {PATH}")

# Configuration Validation
if "gemini" in MODEL_NAME.lower() and not os.getenv("GEMINI_API_KEY"):
    print(f"[bold red]Error:[/] GEMINI_API_KEY not found in .env for model {MODEL_NAME}")
elif "gpt" in MODEL_NAME.lower() and not os.getenv("OPENAI_API_KEY"):
    print(f"[bold red]Error:[/] OPENAI_API_KEY not found in .env for model {MODEL_NAME}")
elif "claude" in MODEL_NAME.lower() and not os.getenv("ANTHROPIC_API_KEY"):
    print(f"[bold red]Error:[/] ANTHROPIC_API_KEY not found in .env for model {MODEL_NAME}")

console = Console()

def get_watching_panel():
    status_text = "[yellow]MOCK MODE ACTIVE[/]" if MOCK_MODE else f"Model: [bold cyan]{MODEL_NAME}[/]"
    return Panel(Spinner("dots", text=f"Watching AzerothLM.lua... {status_text}"), title="AzerothLM Relay", border_style="green")

def get_dashboard(main_content):
    mode_color = "yellow" if MOCK_MODE else "green"
    mode_text = "MOCK" if MOCK_MODE else "LIVE"
    mode_panel = Panel(f"Current Mode: [bold {mode_color}]{mode_text}[/]", border_style=mode_color)
    return Group(mode_panel, main_content)

with Live(get_dashboard(get_watching_panel()), refresh_per_second=10) as live:
    while True:
        live.update(get_dashboard(get_watching_panel()))
        
        try:
            if os.path.exists(PATH):
                with open(PATH, 'r', encoding='utf-8') as f:
                    content = f.read()
            else:
                content = ""
        except Exception as e:
            time.sleep(5)
            continue

        if "AzerothLM_DB" not in content:
            time.sleep(5)
            continue

        # Check for IDLE status to reset the last processed query
        if re.search(r'\["status"\]\s*=\s*"IDLE"', content):
            last_processed_query = None

        # Regex Pre-check: Only proceed if status is SENT
        if not re.search(r'\["status"\]\s*=\s*"SENT"', content):
            time.sleep(5)
            continue

        try:
            parser = LuaParser(content)
            db = parser.parse()
        except Exception as e:
            time.sleep(5)
            continue

        if db and db.get("status") == "SENT":
            if db.get("currentChatID") in [None, ""] or db.get("chats") in [None, ""]:
                time.sleep(5)
                continue

            lock = FileLock(LOCK_PATH)
            try:
                with lock.acquire(timeout=0):
                    # 1. Identify currentChatID
                    chat_id = int(db.get("currentChatID", 1))
                    chats = db.get("chats", {})
                    
                    # Handle chats as list or dict depending on parsing
                    current_chat = None
                    if isinstance(chats, dict):
                        if 1 in chats:
                            current_chat = chats.get(chat_id)
                        else:
                            current_chat = chats.get(chat_id)
                    
                    if current_chat:
                        chat_name = current_chat.get("name", "Unknown")
                        messages = current_chat.get("messages", {})
                        
                        # 2. Extract last user message
                        last_idx = 0
                        last_msg = None
                        if isinstance(messages, dict) and messages:
                            last_idx = max(k for k in messages.keys() if isinstance(k, int))
                            last_msg = messages[last_idx]
                        
                        if last_msg and last_msg.get("sender") == "You":
                            user_query = last_msg.get("text")
                            
                            if user_query == last_processed_query:
                                info_text = Text.from_markup(f"Chat: [bold cyan]{chat_name}[/]\nSkipping redundant query: {user_query}")
                                live.update(get_dashboard(Panel(info_text, title="Skipping Redundant Request", border_style="yellow")))
                                time.sleep(2)
                                continue

                            last_processed_query = user_query
                            
                            # Decode Context
                            context = {}
                            
                            # Gear
                            gear = db.get("gear", {})
                            decoded_gear = {}
                            if isinstance(gear, dict):
                                 for k, v in gear.items():
                                     if v: decoded_gear[str(k)] = decode_hex(v)
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
                                        if "name" in p_new: p_new["name"] = decode_hex(p_new["name"])
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
                                        if "title" in q_new: q_new["title"] = decode_hex(q_new["title"])
                                        decoded_quests.append(q_new)
                            context["quests"] = decoded_quests

                            context_str = str(context)
                            context_size = len(context_str.encode('utf-8'))

                            # Update Display: Processing
                            info_text = Text.from_markup(f"Chat: [bold cyan]{chat_name}[/]\nModel: [bold magenta]{MODEL_NAME}[/]\nQuery: {user_query}\nContext Size: [bold]{context_size}[/] bytes\n\n")
                            info_text.append(Text("Calling AI...", style="yellow"))
                            live.update(get_dashboard(Panel(info_text, title="Processing Request", border_style="yellow")))

                            # 3. Call AI
                            ai_response = call_ai(user_query, context_str, chat_name, live)
                            
                            # 4. Safety Buffer with Progress Bar
                            prog = Progress(
                                BarColumn(),
                                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                                TimeRemainingColumn()
                            )
                            task_id = prog.add_task("Safety Buffer", total=40)
                            
                            for _ in range(40):
                                prog.advance(task_id)
                                info_text = Text.from_markup(f"Chat: [bold cyan]{chat_name}[/]\nModel: [bold magenta]{MODEL_NAME}[/]\nQuery: {user_query}\nContext Size: [bold]{context_size}[/] bytes\n\n")
                                info_text.append(Text("Response Received. Waiting for Safety Buffer...\n", style="blue"))
                                live.update(get_dashboard(Panel(Group(info_text, prog), title="Safety Buffer", border_style="blue")))
                                time.sleep(0.1)

                            # 5. Re-read file to verify status and get latest state
                            with open(PATH, 'r', encoding='utf-8') as f:
                                current_content = f.read()
                            parser = LuaParser(current_content)
                            latest_db = parser.parse()

                            if latest_db and latest_db.get("status") == "SENT":
                                # Inject response into latest_db
                                l_chat_id = int(latest_db.get("currentChatID", 1))
                                l_chats = latest_db.get("chats", {})
                                if isinstance(l_chats, dict) and l_chat_id in l_chats:
                                    l_chat = l_chats[l_chat_id]
                                    l_msgs = l_chat.get("messages", {})
                                    next_id = max([k for k in l_msgs.keys() if isinstance(k, int)] or [0]) + 1
                                    l_msgs[next_id] = {"sender": "AI", "text": ai_response}
                                    l_chat["messages"] = l_msgs
                                    
                                    latest_db["status"] = "COMPLETE"
                                    temp_path = PATH + ".tmp"
                                    with open(temp_path, 'w', encoding='utf-8') as f:
                                        f.write('AzerothLM_DB = ' + to_lua(latest_db))
                                    os.replace(temp_path, PATH)
            except Timeout:
                time.sleep(2)
                continue
            except Exception as e:
                time.sleep(5)
                continue
        
        time.sleep(5)