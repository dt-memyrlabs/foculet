# Foculet

**The parking lot for your attention.** Foculet watches which window you're
using on each monitor. The moment you switch away from one, it parks that
window — minimized, with a snapshot pinned to a picture board on a monitor
you designate as the dump. Click a picture and the window comes back exactly
where it was. Close a picture and the window is gone for good.

No time thresholds, no "are you still working?" popups. Every switch parks.
Your dump monitor is sacred ground: a live parking lot, not a workspace.

## How it works

1. **Focus watcher** (background thread, polls every 0.5s). Each monitor
   remembers its *current* window. When a new window takes focus on a
   monitor, that monitor's previous window is eligible for parking.
2. **Park.** Foculet snapshots the window (`PrintWindow` — works even for
   windows behind other windows), minimizes the real window, and pins the
   snapshot to the board.
3. **The board.** A borderless Tkinter window covering the dump monitor,
   showing parked windows oldest-first in a configurable grid. Hidden when
   empty, always at the bottom of the z-order, and it can never steal
   keyboard focus.
4. **Restore.** Click a picture (or its title, or its cell) and the real
   window un-minimizes back to its original monitor, position, size, and
   maximized state. Right-click a picture to close the real window instead —
   Foculet notices it's gone and drops the picture.
5. **Glance protection.** A parked window that briefly becomes foreground
   (Alt+Tab preview, taskbar hover peek, an app raising itself for a moment)
   stays parked unless it holds focus for a full second.

### Chrome tabs

A Chrome extension watches tab switches in the focused Chrome window. When
you move to a new tab, the previous tab is *torn off* into its own window
(`chrome.windows.create({tabId})`) and parked through the normal flow.
Restoring brings the window back — it is not re-attached to the original
window. The extension talks to a tiny local bridge server
(`bridge-server.py`, HTTP on `127.0.0.1:18721`); Foculet polls it for tab
state and sends it detach/close commands.

## Components

| File | What it is |
|---|---|
| `foculet.py` | The whole app: watcher, parker, board, tab logic |
| `bridge-server.py` | Localhost HTTP bridge (`127.0.0.1:18721`) the extension long-polls for commands |
| `extension/background.js` | Chrome extension service worker: tab watcher, tab tear-off |
| `extension/manifest.json` | Extension manifest (Manifest V3) |
| `ctl.ps1` | `status` / `stop` / `start` control script for Windows |
| `foculet.json` | Config, created on first run (dump monitor, grid, exclusions) |

## Requirements

- Windows 10 or 11
- Python 3.10+ with `pywin32`, `Pillow`, and `pystray` (`pip install pywin32 pillow pystray`)
- Google Chrome (only needed for tab parking; the window parker works alone)

## Install

```powershell
git clone https://github.com/dt-memyrlabs/foculet.git
cd foculet
pip install pywin32 pillow pystray

# 1. Bridge server (leave running) — lets the extension talk to Foculet
python bridge-server.py

# 2. Chrome extension — chrome://extensions → Developer mode →
#    "Load unpacked" → select the extension/ folder

# 3. Foculet itself (no console window - it lives in the system tray)
.\ctl.ps1 -Action start
```

The tray icon (bottom-right) shows the parked count, and its menu has
Pause parking / Resume parking and Exit. Right-click it anytime.

On first run Foculet shows a small monitor picker if it can't tell which
monitor is the dump — pick one and it's saved to `foculet.json`.

`.\ctl.ps1 -Action status` checks it's running; `-Action stop` kills it.

## Configuration (`foculet.json`)

| Key | Meaning | Default |
|---|---|---|
| `dump_device` | Monitor that hosts the board (`\\.\DISPLAY2`…) | chosen at first run |
| `grid_cols` / `grid_rows` | Board grid, 1–8 each; board capacity = cols × rows | 3 × 2 |
| `excluded_exes` | Process names (e.g. `notepad.exe`) that are never parked | `[]` |

Delete `foculet.json` to re-run first-time setup.

## The rules — what never gets parked

- **Fullscreen windows.** Games, fullscreen video, presentations are untouched.
- **Dialogs and popups.** File pickers, save dialogs, dropdowns — anything
  *owned* by another window — never count as a switch and are never parked
  themselves. Opening a dialog leaves the window behind it exactly where it is.
- **Transient shell UI.** The system tray overflow, Start menu, search,
  notification center, and volume/network flyouts are invisible to the
  watcher: opening them neither parks your current window nor parks
  themselves. (Explorer *file* windows still park normally.)
- **The dump monitor.** Windows already on the board's monitor are left alone.
- **Excluded apps**, the Foculet console, and windows without titles.
- **Board capacity.** When the grid is full, further parks are skipped
  (oldest-parked-first ordering keeps the board a readable timeline).

## Anti-cascade design

Parking *moves windows*, and moving windows moves focus — which the watcher
could read as another switch, causing a runaway park → focus-jump → park
loop (this actually happened with file dialogs: every dialog opened parked
the window behind it, which jumped focus, which parked something else…).
Three guards break the loop:

1. **Owned windows are invisible to the watcher.** A dialog opening is not a
   switch, full stop.
2. **Quiet period.** For 1.5s after Foculet minimizes or restores a window
   itself, focus changes are absorbed, not acted on. A real user switch
   inside that window is simply not parked — it fails open toward leaving
   your windows alone.
3. **The board can never activate.** It carries `WS_EX_NOACTIVATE`, so
   showing or rebuilding it can never steal focus and register as a switch.

## Multi-monitor behavior

- Each monitor tracks its own current window independently.
- Parking only ever triggers for the monitor the *new* focus landed on.
- Restoring returns the window to its origin monitor and geometry.
- The dump monitor defaults to the rightmost display; any monitor works.

## Privacy

Everything is local. The only network traffic is the extension talking to
`127.0.0.1:18721` on your own machine. No accounts, no cloud, no telemetry.

## Limitations

- Board pictures are snapshots, not live views.
- A very fast switch inside the 1.5s quiet window won't park the window you
  left (fail-open, by design).
- Windows only. Chrome tab parking needs the extension + bridge server
  running; without them, Foculet still parks whole Chrome windows.
- Minimized windows with a modal dialog open can confuse Windows' focus
  bookkeeping — the owned-window guard keeps Foculet out of the way, but the
  OS may still shuffle z-order.

## Project layout

```
foculet/
├── foculet.py            # the app
├── bridge-server.py      # localhost bridge for the extension
├── extension/
│   ├── manifest.json
│   └── background.js
├── ctl.ps1               # status | stop | start
├── foculet.json          # created on first run
├── README.md
└── LICENSE               # MIT
```

## License

MIT — see `LICENSE`. Copyright (c) 2026 Daniel Thomas.
