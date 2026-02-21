# AzerothLM Alpha Test — 21 MAR 2026

AzerothLM is a WoW TBC Classic addon + Python relay that lets you ask an AI context-aware questions about your character using a research journal.

## Setup

### 1. Install the Addon

Copy these 3 files into your WoW AddOns directory:

```
Interface/AddOns/AzerothLM/
  AzerothLM.toc
  AzerothLM.lua
  AzerothLM_UI.lua
```

### 2. Install Python Dependencies

Make sure you have Python 3.x installed, then run:

```bash
pip install -r requirements.txt
```

### 3. Configure the Relay

1. Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```
2. Open `.env` in a text editor and update:
   - `WOW_SAVED_VARIABLES_PATH` — path to your WoW account's `SavedVariables/AzerothLM.lua` file
   - `WOW_ADDON_PATH` — path to your `Interface/AddOns/AzerothLM` folder
3. Add an API key using one of these methods:
   - **Manual**: Paste your key into `.env` (e.g., `GEMINI_API_KEY=your_key_here`)
   - **Interactive**: Start the relay first, then use the `/model add` command

> Free API keys: [Google AI Studio](https://aistudio.google.com/) (Gemini), [OpenAI](https://platform.openai.com/api-keys), [Anthropic](https://console.anthropic.com/)

## Start the Relay

```bash
python AzerothLM_Relay.py
```

You should see a startup banner showing your active model and configured providers. If a provider shows "Ready", you're good to go.

## Test Script

Run these commands in order in the relay CLI. Report any errors or unexpected behavior.

### Phase 1: Test Mode (no API credits used)

1. **`/test on`** — Should show a config check panel. Verify all items show PASS.
2. **`/new "Alpha Test"`** — Should create a topic with slug `alpha-test`.
3. **`/ask alpha-test "What gear should I upgrade?"`** — Should return a response starting with `[TEST MODE]`.
4. **`/topics`** — Should list "Alpha Test" with 1 entry.
5. **`/view alpha-test`** — Should show the full Q&A (your question + the mock answer).
6. **`/context`** — Should show character context JSON (may be empty if you haven't done `/alm scan` in-game yet).

### Phase 2: Live API (uses real tokens)

7. **`/test off`** — Disables test mode.
8. **`/ask alpha-test "Best professions for making gold in TBC?"`** — Should return a real AI response (takes a few seconds).
9. **`/usage`** — Should show 1 API call and token counts.

### Phase 3: Model Management

10. **`/model`** — Should show a table of all providers with status (Ready / Not configured).
11. **`/model switch`** — Try switching to a different model within your configured provider.
12. **`/status`** — Verify the active model updated.

### Phase 4: Cleanup

13. **`/delete alpha-test`** — Delete the test topic.
14. **`/topics`** — Should show no topics.

### Phase 5: In-Game (requires WoW running)

15. Start the relay and create a topic with `/new` and `/ask` as above.
16. Log into WoW and type **`/alm`** to open the journal window.
17. Type **`/alm scan`** to capture your character's gear, professions, and quests.
18. Type **`/reload`** to sync — the journal should show your topic and Q&A entries.
19. Right-click a topic in the sidebar for context menu options.
20. Use mouse wheel to scroll through long responses.

### Phase 6: Exit

21. **`/quit`** — Exit the relay cleanly.

## Bug Reports

When reporting a bug, please include:

- **Steps to reproduce** — what commands you ran, in what order
- **Expected vs actual behavior** — what you thought would happen vs what did happen
- **Relay console output** — copy/paste any error messages or unexpected output
- **Environment** — OS, Python version (`python --version`), WoW client version
