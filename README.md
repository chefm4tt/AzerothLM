# AzerothLM

**AzerothLM** is a World of Warcraft: TBC Classic addon that bridges the gap between the game client and modern Large Language Models (LLMs). It allows players to converse with an AI assistant directly in-game, providing context-aware advice based on their character's current state.

## Project Overview: The 'Air-Gap' Architecture

World of Warcraft addons run in a sandboxed Lua environment with no direct internet access. To overcome this, AzerothLM utilizes a file-based "Air-Gap" architecture:

1.  **The Addon**: Writes the user's query and character data to the `SavedVariables` file on the hard drive.
2.  **The Relay**: A Python script (`AzerothLM_Relay.py`) constantly monitors this file for changes.
3.  **The AI**: When a request is detected, the relay sends the data to the Gemini API via `litellm`.
4.  **The Response**: The relay writes the AI's response back into the `SavedVariables` file.
5.  **The Sync**: The user clicks "Sync" in-game (triggering a UI reload) to read the updated file and display the response.

## Key Features

*   **Context Aware**: Automatically detects your character's equipped gear, profession levels, and active quest log. This data is sent to the AI to provide highly specific advice (e.g., "What gear should I upgrade next?").
*   **Multi-Tabbed Chat**: Maintain multiple conversation threads simultaneously with a tabbed interface.
*   **Dynamic Tab Naming**: Rename chat tabs to organize your theory-crafting or quest help sessions.
*   **Terminal UI**: A clean, movable in-game window with a scrolling history, color-coded messages (Cyan for You, Green for AI), and status indicators.
*   **Relay Dashboard**: A modern, real-time CLI dashboard for the Python relay script that shows connection status, current model, and request processing progress.

### Development & Mock Mode

*   **Mock Mode**: Developers can enable `MOCK_MODE` in the `.env` to test the Lua-to-Python handshake and UI stability without consuming API quota.
*   **Context Size**: The relay now provides a real-time **Context Size** indicator to monitor the data weight of gear and quests.

## Requirements

*   **Game**: World of Warcraft: TBC Classic (or compatible client).
*   **Runtime**: Python 3.x
*   **Python Libraries**: `litellm`, `luadata`, `python-dotenv`, `filelock`, `rich`.

## Installation

1.  **Addon Setup**:
    *   Copy the `AzerothLM` folder into your WoW AddOns directory (e.g., `_anniversary_/Interface/AddOns/`).

2.  **Python Environment**:
    *   Install the required libraries:
        ```bash
        pip install -r requirements.txt
        ```

3.  **API Configuration**:
    *   Rename the provided `.env.example` file to `.env`.
    *   Open `.env` in a text editor.
    *   **Required**: Update `WOW_SAVED_VARIABLES_PATH` to point to your specific WoW account folder.
    *   **Required**: Paste your API key (e.g., `GEMINI_API_KEY`) into the appropriate field.
    *   **Optional**: Uncomment the `MODEL_NAME` you wish to use (defaults to Gemini 1.5 Flash).

## Usage

1.  **Start the Relay**:
    *   Run the Python script before or during your play session:
        ```bash
        python AzerothLM_Relay.py
        ```
2.  **In-Game**:
    *   Log into your character.
    *   Type `/alm` to open the AzerothLM Terminal.
    *   Type your question into the input box and press **Enter**. The status will change to `Thinking...`.
    *   Wait a few seconds for the external script to process the request.
    *   Click the **Sync** button in the terminal. This will reload your UI to fetch the response.
    *   The AI's reply will appear in the chat history.

## Development Note

> **Disclaimer**: This project is currently in a **Private Dev** state. It is a proof-of-concept and is not yet stable for public release. The "Sync" mechanism requires a UI reload, which may be disruptive during combat or critical gameplay. Use at your own risk.