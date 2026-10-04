# Ragnarok private-server auto hunter

A screen-based Windows automation prototype for a Ragnarok Online private server.

It does **not** modify the game client or inject packets. It captures the Ragnarok window, detects configured monster templates, clicks the nearest target, estimates HP from the on-character HP bar, and can press configurable heal/emergency keys.

## Current features

- Find the Ragnarok window automatically
- Screenshot only the game client
- Template-match one or more monster types
- Prefer the closest detected target
- Ignore chat, status UI and minimap/quest areas
- Estimate HP from the green bar below the character
- Heal below a configurable threshold
- Emergency key at critical HP
- Random search movement if no target is found
- F11 pause/resume
- F12 emergency stop
- PyAutoGUI mouse failsafe: move the mouse to the top-left corner

## Setup

Install Python 3.11+ on Windows.

Then from this repository:

```powershell
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Add at least one cropped monster image to the `templates` folder. See [templates/README.md](templates/README.md).

## Configure keys

Open `config.yaml`.

The defaults currently use:

- Heal: `F8`
- Emergency/teleport: `F9`
- Pause: `F11`
- Stop: `F12`

Change these to match your Ragnarok hotbar before running the bot.

## Run

1. Start Ragnarok.
2. Enter the map you want to test on.
3. Make sure the monster template exists in `templates/`.
4. From PowerShell:

```powershell
.venv\Scripts\activate
python main.py
```

Keep your normal client zoom and UI layout while testing.

## Initial calibration

The supplied defaults are based on the 1600×900 screenshot used while building this prototype.

The important values live in `config.yaml`:

- `player.center_x_ratio`
- `player.center_y_ratio`
- `hp.roi`
- `targeting.excluded_regions`

The bot uses ratios, so resizing can still work reasonably, but recalibration may be needed.

## First test recommendation

For the first run, temporarily set:

```yaml
movement:
  enabled: false
```

Put your character near one target monster and confirm the console repeatedly detects it correctly. After targeting works, enable movement.

## Safety

This project sends real keyboard/mouse input. Test on your own server with an expendable character first. F12 stops the bot, and PyAutoGUI's top-left mouse failsafe remains enabled.
