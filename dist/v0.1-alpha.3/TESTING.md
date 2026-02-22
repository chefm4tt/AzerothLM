# AzerothLM v0.1-alpha.3 — Tester Setup Guide

## What's New in This Version

- **Context-aware prompts**: Relay filters character context to only the sections relevant to each question (gear, quests, professions, etc.) — faster, more focused AI responses
- **Cache fix**: Cache keys now incorporate conversation history so follow-up questions always get fresh responses
- **Help system rework**: `/help` shows a categorized command overview; `/help <cmd>` shows a detailed panel for any command
- **First-run setup**: Relay detects missing paths on startup and walks you through configuration interactively

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

Edit `.env` and fill in your API key and WoW paths — or just run the relay and let the first-run setup guide you:
```bash
python AzerothLM_Relay.py
```

If required paths are missing, the relay will prompt you to enter them interactively and save them to `.env`.

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

## Test Script — v0.1-alpha.3

Work through these tests in order. Note any failures or unexpected behavior.

### Phase 1 — First-Run Setup

| # | Action | Expected Result |
|---|--------|----------------|
| 1 | Start relay with a blank `.env` (no paths set) | Relay detects missing paths and prompts you to enter them interactively |
| 2 | Enter your `WOW_SV_PATH` when prompted | Relay saves it to `.env` and continues startup |

> If you already have a working `.env`, skip to Phase 2.

### Phase 2 — Basic Flow

| # | Action | Expected Result |
|---|--------|----------------|
| 3 | Start relay (`python AzerothLM_Relay.py`) | Relay starts, shows configured providers and active model |
| 4 | Type `/new My First Topic` | Prompt confirms topic created |
| 5 | Type `/ask What class should I play in TBC?` | AI response appears; relay used general context only (no gear/quest dump) |
| 6 | Type `/ask How is my gear for my level?` | AI response includes gear context; relay filtered to gear-relevant sections |
| 7 | Type `/topics` | Shows "My First Topic" in the list |
| 8 | Switch to WoW, type `/reload`, then `/alm` | Journal window opens, shows topic and responses |

### Phase 3 — Cache Behavior

| # | Action | Expected Result |
|---|--------|----------------|
| 9 | Ask the same question twice in a row | Second response returns immediately (cache hit) |
| 10 | Ask the same question again after the AI has added a new entry | Response is freshly generated (cache miss — history changed) |

### Phase 4 — Help System

| # | Action | Expected Result |
|---|--------|----------------|
| 11 | Type `/help` | Shows categorized table of all CLI commands |
| 12 | Type `/help ask` | Shows detailed panel for `/ask` command |
| 13 | Type `/help model` | Shows detailed panel for `/model` command |

### Phase 5 — Journal Management

| # | Action | Expected Result |
|---|--------|----------------|
| 14 | `/delete My First Topic` | Confirms deletion |
| 15 | In-game: `/reload`, then `/alm` | Topic no longer appears |
| 16 | In-game: `/alm wipe` | Prompts confirmation; journal is empty after confirming |
| 17 | In-game: `/alm reset` | Rebuilds journal from relay-cached signal |
| 18 | In-game: `/alm help` | Shows list of in-game commands in chat |

### Phase 6 — CLI Commands

| # | Action | Expected Result |
|---|--------|----------------|
| 19 | `/model list` | Shows all configured providers and models |
| 20 | `/model switch` | Interactive provider → model selection |
| 21 | `/test on` | Relay enters test mode; next `/ask` returns mock response with `[TEST MODE]` prefix |
| 22 | `/test off` | Returns to live mode |
| 23 | `/context` | Shows parsed character data (class, level, gear, professions) |
| 24 | `/status` | Shows current model, testing mode, provider status |

---

## Reporting Issues

Please include:
1. What you did (exact command or action)
2. What you expected
3. What actually happened
4. Your `debug.log` if the relay was running with `--debug`

Send feedback to the developer or post in the alpha test channel.
