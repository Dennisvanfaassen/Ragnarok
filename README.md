# Ragnarok Auto Hunter

Screen-based automation for your own Ragnarok Online private server.

## Current behavior

- Finds the real `Classic.exe` game window
- Works with the client in windowed mode
- Uses client-relative coordinates, so moving the game window on the desktop is fine
- Uses native Windows mouse events for target clicks and movement
- Detects Obeaune and Cornutus using multiple visual matching methods
- Clicks the closest detected monster
- Re-clicks if needed after a cooldown
- Walks around when no monster is visible
- Performs a loot sweep around the death position when a target disappears
- Your Soulbound server also reports `Pet auto-loot enabled`, which should remain enabled because that is the most reliable way to collect every drop
- F10 saves a detection debug screenshot
- F11 pauses/resumes
- F12 stops

## Install / update

After downloading a fresh ZIP from GitHub:

1. Extract it.
2. Double-click `setup_bot.bat`.
3. Start Soulbound Journey and log in.
4. Double-click `run_bot.bat`.

## Windowed mode

Windowed mode is supported. The client can sit anywhere on the screen. The bot converts its detected client coordinates to the current Windows screen position before clicking.

## Looting

Two layers are used:

1. Keep Soulbound's **Pet auto-loot** enabled. Your client chat has already shown `Pet auto-loot enabled`.
2. When the monster disappears, the bot also performs a small click sweep around its last known position to pick up anything that remains on the ground.

The sweep can be configured under `loot:` in `config.yaml`.

## Healing

Healing remains disabled until the correct hotkeys are configured:

```yaml
hp:
  enabled: false
```

## Debug controls

- F10: writes `debug_last.jpg`
- F11: pause/resume
- F12: stop

If the console says `-> CLICK` but the game still does not react, send the console output and a screenshot while the bot is running.
