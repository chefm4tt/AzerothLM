# AzerothLM v0.1-alpha.2 — Tester Setup Guide

## What's New in This Version

- **Journal management commands**: `/alm reset`, `/alm wipe`, `/alm help` in-game
- **CLI improvements**: Better error messages, input validation, contextual hints
- **Holistic `--debug` mode**: Full diagnostic logging across all subsystems
- **Context pipeline overhaul**: Item quality colorization, improved gear/quest display
- **Performance**: mtime-based SavedVariables caching, in-memory response cache, atomic file writes, conditional signal writes

---

## Installation

### Prerequisites
- WoW TBC Classic / Anniversary Edition
- Python 3.x
- An API key for at least one supported provider (Gemini, OpenAI, Anthropic) — or Ollama running locally

### 1. Install the Addon

Copy the `AzerothLM/` folder into your WoW AddOns directory:

```
C:\Program Files (x86)\World of Warcraft\_anniversary_\Interface\AddOns\AzerothLM\
```

> The folder structure should be: `AddOns\AzerothLM\AzerothLM.toc`

### 2. Set Up the Relay

Open a terminal in the `AzerothLM/` folder (where `AzerothLM_Relay.py` lives).

Install dependencies:
```bash
pip install -r requirements.txt
```

Copy the config template:
```bash
cp .env.example .env
```

Edit `.env` and fill in your API key and WoW paths:
```
MODEL_NAME=gemini/gemini-2.0-flash          # or openai/gpt-4o, anthropic/claude-sonnet-4-6
GEMINI_API_KEY=your-key-here                 # whichever provider you chose
WOW_SV_PATH=C:/Users/<YourName>/AppData/Roaming/...\AzerothLM.lua
```

> **Finding `WOW_SV_PATH`:** Look in `WTF/Account/<AccountName>/SavedVariables/AzerothLM.lua` under your WoW install directory.

### 3. Enable the Addon In-Game

Log into WoW, open **AddOns** at character select, and enable **AzerothLM**.

### 4. Scan Your Character

In-game, type `/alm scan` to let the addon capture your character context (gear, professions, quests). This only needs to be done once per session or when your character changes significantly.

---

## Running the Relay

### CLI Mode (recommended for testing)
```bash
python AzerothLM_Relay.py
```

### MCP Mode (for Claude Code integration)
```bash
python AzerothLM_Relay.py --mcp
```

### Debug Mode (send this log if reporting issues)
```bash
python AzerothLM_Relay.py --mcp --debug 2>>debug.log
```

---

## Test Script — v0.1-alpha.2

Work through these tests in order. Note any failures or unexpected behavior.

### Phase 1 — Basic Flow

| # | Action | Expected Result |
|---|--------|----------------|
| 1 | Start relay (`python AzerothLM_Relay.py`) | Relay starts, shows configured providers and active model |
| 2 | Type `/new My First Topic` | Prompt confirms topic created |
| 3 | Type `/ask What class should I play in TBC?` | AI response appears in terminal |
| 4 | Type `/topics` | Shows "My First Topic" in the list |
| 5 | Switch to WoW, type `/reload`, then `/alm` | Journal window opens, shows topic and response |

### Phase 2 — Journal Management

| # | Action | Expected Result |
|---|--------|----------------|
| 6 | In-game: `/alm topics` | Lists topics in chat |
| 7 | In CLI: `/delete My First Topic` | Confirms deletion |
| 8 | In-game: `/reload`, then `/alm` | Topic no longer appears |
| 9 | In-game: `/alm wipe` | Prompts confirmation; after confirming, journal is empty |
| 10 | In-game: `/alm reset` | Rebuilds journal from relay-cached signal |
| 11 | In-game: `/alm help` | Shows list of in-game commands in chat |

### Phase 3 — CLI Commands

| # | Action | Expected Result |
|---|--------|----------------|
| 12 | `/model list` | Shows all configured providers and models |
| 13 | `/model switch` | Interactive provider → model selection |
| 14 | `/test on` | Relay enters test mode; next `/ask` returns mock response with `[TEST MODE]` prefix |
| 15 | `/test off` | Returns to live mode |
| 16 | `/context` | Shows parsed character data (class, level, gear, professions) |
| 17 | `/status` | Shows current model, testing mode, provider status |
| 18 | `/help` | Lists all CLI commands |

### Phase 4 — Performance (optional but valuable)

| # | Action | Expected Result |
|---|--------|----------------|
| 19 | Ask the same question twice | Second response returns immediately (cache hit) |
| 20 | Start relay with `--debug 2>>debug.log`, run a few commands, check `debug.log` | Log shows timestamps, `[DBG]` entries for each operation |

---

## Reporting Issues

Please include:
1. What you did (exact command or action)
2. What you expected
3. What actually happened
4. Your `debug.log` if the relay was running with `--debug`

Send feedback to the developer or post in the alpha test channel.
