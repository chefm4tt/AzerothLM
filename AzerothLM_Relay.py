import re
import os
import sys
import time
import luadata
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
MODEL_NAME = os.getenv("MODEL_NAME", "gemini/gemini-1.5-flash")

# Path to your SavedVariables file
PATH = os.getenv("WOW_SAVED_VARIABLES_PATH")

if not PATH or "YOUR_ACCOUNT_NAME" in PATH:
    print("Error: WOW_SAVED_VARIABLES_PATH is missing or invalid in .env")
    print("Please update it to point to your actual World of Warcraft Account folder.")
    sys.exit(1)

PATH = os.path.normpath(PATH)
LOCK_PATH = PATH + ".lock"

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

def call_ai(user_query, game_context, chat_name):
    # Universal system instruction for all models
    system_instruction = (
        "You are a specialized AI assistant for World of Warcraft: The Burning Crusade Classic. "
        "Use the provided JSON context (gear, professions, quests) to give specific, actionable advice. "
        "Do not ask what game the user is playing; assume it is always TBC Classic. "
        "Format your responses using simple line breaks for compatibility with the WoW UI."
    )

    try:
        response = completion(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": f"The user is currently in a chat session titled '{chat_name}'. Prioritize information relevant to this topic while still considering their overall character data.\n\nContext: {game_context}\n\nQuestion: {user_query}"}
            ]
        )
        return response.choices[0].message.content or ""
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
    return Panel(Spinner("dots", text="Watching AzerothLM.lua..."), title="AzerothLM Relay", border_style="green")

with Live(get_watching_panel(), refresh_per_second=10) as live:
    while True:
        live.update(get_watching_panel())
        
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
                            
                            # Update Display: Processing
                            info_text = Text.from_markup(f"Chat: [bold cyan]{chat_name}[/]\nModel: [bold magenta]{MODEL_NAME}[/]\nQuery: {user_query}\n\n")
                            info_text.append(Text("Calling AI...", style="yellow"))
                            live.update(Panel(info_text, title="Processing Request", border_style="yellow"))

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

                            # 3. Call AI
                            ai_response = call_ai(user_query, str(context), chat_name)
                            
                            # 4. Safety Buffer with Progress Bar
                            prog = Progress(
                                BarColumn(),
                                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                                TimeRemainingColumn()
                            )
                            task_id = prog.add_task("Safety Buffer", total=40)
                            
                            for _ in range(40):
                                prog.advance(task_id)
                                info_text = Text.from_markup(f"Chat: [bold cyan]{chat_name}[/]\nModel: [bold magenta]{MODEL_NAME}[/]\nQuery: {user_query}\n\n")
                                info_text.append(Text("Response Received. Waiting for Safety Buffer...\n", style="blue"))
                                live.update(Panel(Group(info_text, prog), title="Safety Buffer", border_style="blue"))
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
                                    luadata.write(temp_path, latest_db, encoding='utf-8', indent='\t', prefix='AzerothLM_DB = ')
                                    os.replace(temp_path, PATH)
            except Timeout:
                time.sleep(2)
                continue
            except Exception as e:
                time.sleep(5)
                continue
        
        time.sleep(5)