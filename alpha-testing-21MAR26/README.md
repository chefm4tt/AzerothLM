# AzerothLM

**AzerothLM** is a World of Warcraft addon and Python relay that bridges the game's sandboxed Lua environment with modern Large Language Models. Players create research topics and ask context-aware questions through an interactive CLI, and view AI responses in a read-only in-game journal.

## Architecture: The "Air-Gap" Bridge

WoW addons run in a sandboxed Lua environment with no internet access. AzerothLM bridges this gap using a file-based relay:

1. **Context Collection**: The addon scans your character's equipped gear, professions, and quest log into `SavedVariables`.
2. **Research Input**: You create topics and ask questions through the relay CLI (or MCP tools).
3. **AI Processing**: The relay sends your question plus character context to an LLM via [LiteLLM](https://docs.litellm.ai/) (supporting multiple providers).
4. **Signal File**: The relay writes the AI response to `AzerothLM_Signal.lua`.
5. **In-Game Sync**: Type `/reload` in-game to load the updated journal.

## Key Features

- **Context-Aware AI** — Automatically reads your equipped gear, profession levels, and active quests to provide specific, actionable advice.
- **Research Journal** — Organize questions into named topics with full Q&A history.
- **Multi-Provider Support** — Switch between Google Gemini, OpenAI, Anthropic, or local Ollama models at runtime.
- **Interactive CLI** — Rich terminal interface with commands for topic management, model switching, and diagnostics.
- **MCP Server Mode** — Run as a [Model Context Protocol](https://modelcontextprotocol.io/) server for integration with Claude Code or other AI agents.
- **In-Game Journal Viewer** — Draggable, scrollable frame with topic navigation, right-click context menus, and mouse wheel support.
- **Runtime Configuration** — Add API keys (`/model add`), switch models (`/model switch`), and toggle test mode (`/test on|off`) without editing files.
- **Test Mode** — Validate your configuration and test the full pipeline without consuming API credits. All responses are prefixed with `[TEST MODE]`.
- **Response Caching** — Cached responses reduce API usage and latency for repeated queries.
- **Rate Limiting** — Built-in cooldown and exponential backoff protect against API rate limits.

## Requirements

- **Game**: World of Warcraft: TBC Classic (Anniversary Edition compatible)
- **Runtime**: Python 3.x
- **Python Libraries**: `litellm`, `python-dotenv`, `filelock`, `rich`, `mcp`

## Installation

### 1. Addon Setup

Copy the `AzerothLM` folder into your WoW AddOns directory:
```
Interface/AddOns/AzerothLM/
```

### 2. Python Environment

Install the required libraries:
```bash
pip install -r requirements.txt
```

### 3. Configuration

1. Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```
2. Edit `.env` and update the two path settings to match your WoW installation:
   - `WOW_SAVED_VARIABLES_PATH` — path to your account's `SavedVariables/AzerothLM.lua`
   - `WOW_ADDON_PATH` — path to the `Interface/AddOns/AzerothLM` folder
3. Add at least one API key:
   - **Option A**: Paste your key directly into `.env` (e.g., `GEMINI_API_KEY=your_key`)
   - **Option B**: Start the relay and use the interactive `/model add` command
4. Optionally change `MODEL_NAME` in `.env`, or use `/model switch` at runtime.

## Usage

### CLI Mode (Primary)

Start the relay:
```bash
python AzerothLM_Relay.py
```

The CLI provides a Rich terminal interface with the following commands:

| Command | Description |
|---------|-------------|
| `/new <title>` | Create a new research topic |
| `/ask <slug> <question>` | Ask a question on a topic |
| `/topics` | List all topics |
| `/view <slug>` | View full Q&A history for a topic |
| `/delete <slug>` | Delete a topic |
| `/model` | Show providers and models |
| `/model add` | Add a new provider API key |
| `/model switch` | Switch to a different model |
| `/test on\|off` | Toggle test mode |
| `/context` | Show character context |
| `/usage` | Show API usage stats |
| `/status` | Show relay configuration |
| `/help` | Show all commands |
| `/quit` | Exit the relay |

### MCP Server Mode

For integration with Claude Code or other MCP-compatible AI agents:
```bash
python AzerothLM_Relay.py --mcp
```

This exposes research journal tools (create topics, ask questions, view history, read character context) as MCP tool calls.

### In-Game

| Command | Description |
|---------|-------------|
| `/alm` | Toggle the journal window |
| `/alm scan` | Refresh character context (gear, professions, quests) |
| `/alm refresh` | Reload UI (shortcut for `/reload`) |
| `/alm topics` | List topics in chat |
| `/alm delentry <N>` | Delete entry N from the current topic |

After using the CLI to create topics and ask questions, type `/reload` in-game to sync the latest data into the journal viewer.

## Testing

Enable test mode to validate your setup without consuming API credits:

```
/test on    — enable test mode, run configuration checks
/test off   — disable test mode
```

When test mode is active, all AI responses are replaced with mock data prefixed with `[TEST MODE]`. The `/test on` command also runs a diagnostic check verifying your `.env` file, API key, SavedVariables path, and addon path.
