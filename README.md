# 🧠 AzerothLM

An AI research companion for World of Warcraft — context-aware, multi-provider, and journal-based.

AzerothLM bridges the game's sandboxed Lua environment with modern Large Language Models. It reads your character's actual state — gear, professions, quests, reputations, zone, gold — and makes that context available to an AI that answers specific, actionable questions about your character, not generic ones.

---

## 💡 What Can You Do With It?

**Pre-raid gear planning**
> You're a level 60 Hunter sitting in Stormwind with a mix of dungeon blues and quest greens. AzerothLM reads every equipped slot and tells you exactly which pieces to upgrade first, which quest rewards you're about to miss, and which dungeon bosses to prioritize — without you typing a single item name.

**Farming route optimization**
> Mining 300, Skinning 306, currently questing in Hellfire Peninsula with 14 active quests. Ask AzerothLM for the best gold-per-hour route and it answers with specifics — which nodes, which mobs, which respawn paths — tailored to your professions and current location.

**Reputation & attunement planning**
> Hostile with The Aldor, Neutral with The Sha'tar, and trying to figure out what to do next. AzerothLM traces your attunement chain, maps out the rep grind, and identifies which of your active quests feed into it — all from your actual standings and quest log.

---

## 🏗️ Architecture: The Air-Gap Bridge

WoW addons run in a sandboxed Lua environment with no internet access. AzerothLM bridges this gap using a file-based relay:

1. **Context Collection** — The addon scans your equipped gear, profession levels, active quests, reputation standings, and talent points into `SavedVariables`.
2. **Research Input** — You create topics and ask questions through the relay CLI or MCP tools.
3. **AI Processing** — The relay sends your question plus full character context to an LLM via [LiteLLM](https://docs.litellm.ai/), supporting multiple providers.
4. **Signal File** — The relay writes the AI response back to `AzerothLM_Signal.lua`.
5. **In-Game Sync** — Type `/reload` in-game to load the updated journal.

---

## ✨ Key Features

- 🤖 **Context-Aware AI** — Reads your equipped gear (all 19 slots), profession levels, active quest log, reputation standings, talent distribution, zone, level, class, and gold to give character-specific answers.
- 📖 **Research Journal** — Organize questions into named topics with full multi-turn Q&A history. The AI remembers the conversation within each topic.
- 🔀 **Multi-Provider Support** — Switch between Google Gemini, OpenAI, Anthropic Claude, or local Ollama models at runtime. No restart required.
- 🖥️ **Interactive CLI** — Rich terminal interface with commands for topic management, model switching, and diagnostics.
- 🔌 **MCP Server Mode** — Run as a [Model Context Protocol](https://modelcontextprotocol.io/) server for integration with Claude Code or any MCP-compatible AI agent.
- 🎮 **In-Game Journal Viewer** — Draggable, scrollable frame with topic navigation, item quality colorization, right-click context menus, and mouse wheel support.
- ⚙️ **Runtime Configuration** — Add API keys (`/model add`), switch models (`/model switch`), and toggle test mode (`/test on|off`) without editing files.
- 🧪 **Test Mode** — Validate your configuration and test the full pipeline without consuming API credits.
- 🔄 **Response Caching** — Identical queries return instantly from cache, saving API usage and latency.
- 🐛 **Debug Mode** — `--debug` flag or `DEBUG=true` in `.env` enables detailed diagnostic logging to help troubleshoot any issues.

---

## 📋 Requirements

- **Game**: World of Warcraft TBC Classic / Anniversary Edition (Interface version 20505)
- **Runtime**: Python 3.10+
- **Python Libraries**: `litellm`, `python-dotenv`, `rich`, `mcp`, `pyfiglet`
- **API Key**: Google Gemini (recommended — free tier available), OpenAI, Anthropic, or a local Ollama instance

---

## ⚙️ Installation

### 1. Addon Setup

Copy the `AzerothLM` folder into your WoW AddOns directory:
```
World of Warcraft/_anniversary_/Interface/AddOns/AzerothLM/
```

### 2. Python Environment

```bash
pip install -r requirements.txt
```

### 3. Configuration

Copy `.env.example` to `.env` and configure:

```bash
cp .env.example .env
```

Edit `.env` with your paths and API key:

```ini
# Path to your account's SavedVariables file
WOW_SAVED_VARIABLES_PATH=C:\...\WTF\Account\YOUR_ACCOUNT\SavedVariables\AzerothLM.lua

# Path to the installed addon folder
WOW_ADDON_PATH=C:\...\Interface\AddOns\AzerothLM

# Add at least one API key (or use /model add at runtime)
GEMINI_API_KEY=your_key_here
MODEL_NAME=gemini/gemini-2.5-flash
```

### 4. First Run

1. Log into WoW, type `/alm scan` to collect your character context, then `/reload`
2. Start the relay: `python AzerothLM_Relay.py`
3. Create your first topic: `/new gear-upgrades`
4. Ask a question: `/ask gear-upgrades What should I upgrade first?`
5. Type `/reload` in-game to see the response in the journal

---

## 🖥️ CLI Mode

```bash
python AzerothLM_Relay.py
```

| Command | Description |
|---------|-------------|
| `/new <title>` | Create a new research topic |
| `/ask <slug> <question>` | Ask a question on a topic |
| `/topics` | List all topics |
| `/view <slug>` | View full Q&A history for a topic |
| `/delete <slug>` | Delete a topic |
| `/model` | Show configured providers and active model |
| `/model add` | Add a new provider API key interactively |
| `/model switch` | Switch to a different model at runtime |
| `/test on\|off` | Toggle test mode (mock responses, no API cost) |
| `/context` | Show current character context |
| `/usage` | Show API usage stats and token counts |
| `/status` | Show relay configuration |
| `/help` | Show all commands |
| `/quit` | Exit the relay |

---

## 🔌 MCP Server Mode

AzerothLM can run as a [Model Context Protocol](https://modelcontextprotocol.io/) server, exposing its research journal tools to any MCP-compatible AI agent — Claude Code, custom apps, or your own tooling.

### Starting the server

```bash
python AzerothLM_Relay.py --mcp

# With diagnostic logging:
python AzerothLM_Relay.py --mcp --debug 2>>debug.log
```

### Claude Code integration

Add to your `.mcp.json`:

```json
{
  "mcpServers": {
    "azerothlm": {
      "command": "python",
      "args": ["path/to/AzerothLM_Relay.py", "--mcp"]
    }
  }
}
```

Once connected, Claude Code can call all six tools directly in conversation — creating topics, asking character-aware questions, and managing your journal without the CLI.

### Exposed Tools

| Tool | Description | Parameters |
|------|-------------|------------|
| `create_topic` | Create a new research topic in the journal | `title: str` |
| `ask_question` | Ask a question with full character context and topic history | `topic_slug: str`, `question: str` |
| `list_topics` | List all topics with entry counts and last-updated timestamps | — |
| `get_topic` | Retrieve the full Q&A history for a topic | `topic_slug: str` |
| `get_character_context` | Read live character data from WoW SavedVariables | — |
| `delete_topic` | Delete a topic and sync the removal to the in-game journal | `topic_slug: str` |

### Character Context

`get_character_context` returns a JSON object with everything the addon has collected. Every `ask_question` call includes this automatically.

| Category | Data |
|----------|------|
| **Player** | Level, class, race, current zone and subzone, gold on hand |
| **Gear** | All 19 equipment slots with item name and ID (Head through Tabard) |
| **Professions** | All professions and weapon skills with current rank and max rank |
| **Quests** | Active quest log with quest ID, title, level, and completion status |
| **Reputations** | All tracked factions with current standing (Hostile → Exalted) |
| **Talents** | Talent point distribution across all three trees |

---

## 🎮 In-Game Commands

After using the CLI to ask questions, type `/reload` to sync responses into the journal.

| Command | Description |
|---------|-------------|
| `/alm` | Toggle the journal window |
| `/alm scan` | Refresh character context (gear, professions, quests, reputations) |
| `/alm refresh` | Reload UI shortcut |
| `/alm topics` | List all topics in chat |
| `/alm delentry <N>` | Delete entry N from the current topic |
| `/alm reset` | Clear and rebuild the journal from the latest relay data |
| `/alm wipe` | Wipe all journal data and queue deletions on the relay side |
| `/alm help` | Show all in-game commands |

---

## 🧪 Test Mode

Validate your setup without consuming API credits:

```
/test on    — enable test mode, run configuration checks
/test off   — disable test mode
```

When active, all AI responses are replaced with mock data prefixed `[TEST MODE]`. The `/test on` command also runs a full diagnostic verifying your `.env`, API key, SavedVariables path, and addon path.

---

## 📋 Changelog

### v0.1-beta.1 *(in testing)*

- 🆕 MIT License added — AzerothLM is now open source
- ✨ Stale model IDs updated in `.env.example` (Anthropic, OpenAI, Gemini)
- ✨ Requirements pinned to minimum versions for reproducible installs
- 🐛 Fixed user-facing "MCP tools" jargon — messages now say "relay CLI"

### v0.1-alpha.3

- 🆕 Context-aware prompts — questions route to relevant character data sections only
- 🆕 First-run interactive path setup — relay guides new users through `.env` configuration
- 🆕 Help system rework — `/help` shows category table, `/help <cmd>` shows detail panel
- 🐛 Fixed response cache stale hits — history fingerprint included in cache key

### v0.1-alpha.2

- 🆕 In-game journal management commands: `/alm reset`, `/alm wipe`, `/alm help`
- 🆕 Item quality colorization in the journal viewer — gear names display in their rarity color
- 🆕 Richer character context: reputation standings and talent distribution now included
- 🆕 Debug mode: `--debug` startup flag and `DEBUG=true` env var for diagnostic logging
- ✨ CLI UX improvements: ASCII gradient header, loading spinners, cleaner output
- ✨ Improved error messages and input validation throughout the CLI
- ✨ Contextual hints when a topic slug isn't found
- 🐛 Fixed signal sync edge cases — empty journal now correctly signals in-game cleanup
- 🐛 Fixed item quality pattern matching — more lenient parsing with name-based fallback

### v0.1-alpha.1

- 🆕 Multi-provider LLM support: Google Gemini, OpenAI, Anthropic, local Ollama
- 🆕 Interactive CLI with `/model add`, `/model switch`, `/test on|off`, `/status`, `/usage`
- 🆕 MCP server mode for AI agent integration
- 🆕 Research journal with named topics and full multi-turn Q&A history
- 🆕 In-game journal viewer: draggable frame, topic tabs, right-click menus, mouse wheel scroll
- 🆕 Response caching and built-in rate limiting with exponential backoff
- 🆕 Air-gap bridge architecture: file-based relay between WoW sandbox and external AI
