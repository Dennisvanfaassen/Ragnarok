# Ragnarok Auto Hunter

Screen-based automation for your own Ragnarok Online private server.

The bot does not inject into Classic.exe and does not alter game packets. It watches the visible game client and sends normal Windows mouse/keyboard input.

## What this version does

- Prefers the real `Classic.exe` process instead of accidentally choosing the Soulbound launcher
- Detects Obeaune and Cornutus from PNG templates
- Uses color + grayscale + edge matching at multiple scales
- Chooses the closest target to the player
- Clicks the target and avoids rapid repeated clicks on the same monster
- Walks/searches automatically when no target is visible
- Avoids the chat, status bar and minimap/quest UI when searching/clicking
- Estimates HP from the bar below your character
- Healing/teleport actions are OFF by default until you configure the correct keys
- F10 saves `debug_last.jpg` with detection boxes
- F11 pauses/resumes
- F12 stops

## Easiest install

Install Python 3.12 on Windows.

Then double-click:

`setup_bot.bat`

Wait until it says setup completed.

## Start the bot

1. Start Soulbound Journey and log your character in.
2. Keep the Ragnarok client visible.
3. Double-click `run_bot.bat`.
4. Leave the PowerShell/console window open.

Controls:

- F10 = save detection debug image
- F11 = pause/resume
- F12 = stop

## Manual start

If needed:

```powershell
cd C:\Ragnarok-main
.venv\Scripts\activate
python main.py
```

## Healing

Healing is intentionally disabled initially:

```yaml
hp:
  enabled: false
```

Once targeting/movement works, configure `heal_key` and `emergency_key`, then change it to `enabled: true`.

## Detection troubleshooting

If a visible Obeaune or Cornutus is not selected, press F10 while the monster is visible. The bot saves:

`debug_last.jpg`

That image shows what the detector currently sees and is the easiest way to tune recognition.

The current settings were calibrated against the 1600x900 Soulbound screenshots supplied while building the bot.
