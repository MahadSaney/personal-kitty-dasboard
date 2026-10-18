# Kitty Board

A live kanban board for a single Spec Kitty mission, opened from the Cursor chat that is
running it. It stands in for the `/spec-kitty.dashboard` command that went away in Spec Kitty 3.2.7.

Each mission gets its own board on its own local port, so you can watch several missions at once.
The board only reads mission files and never writes to your repo. It runs until you stop it.

## Requirements

- Python 3.10 or newer. Nothing to `pip install`; it uses the standard library only.
- Cursor, for the `/kitty-dashboard` slash command.
- A repo with Spec Kitty missions (`kitty-specs/<mission>/status.events.jsonl`).

## Install

```bash
git clone https://github.com/MahadSaney/personal-kitty-dasboard.git
cd personal-kitty-dasboard
python install.py
```

On Windows, use `py install.py` (or `py -3.12 install.py`) if `python` isn't on your PATH.

The installer writes `~/.cursor/commands/kitty-dashboard.md`, pointing at this clone and at
the Python you ran it with. Rerun it if you move the clone or switch Python.
Remove the command with `python install.py --uninstall`.

## Use

In the Cursor chat where an agent is working on a mission:

| Command | What it does |
|---|---|
| `/kitty-dashboard` | Opens the board for the mission this chat is driving |
| `/kitty-dashboard <slug, mid8 or mission_id>` | Opens the board for a specific mission |
| `/kitty-dashboard stop` | Stops this mission's board |
| `/kitty-dashboard stop all` | Stops every board |
| `/kitty-dashboard list` | Lists running boards |

If the agent can't tell which mission you mean and the repo has several active ones, it asks.

You can also run the script directly:

```bash
python kitty_dashboard/kitty_dashboard.py start --repo /path/to/repo --mission <slug>
python kitty_dashboard/kitty_dashboard.py list
python kitty_dashboard/kitty_dashboard.py stop --all
```

## What's on the board

- One column per lane, from Planned through Done. Blocked and Canceled appear only when they're used.
- Cards with subtask progress, the latest note, the review verdict, the assigned agent and dependency chips.
- A banner when the agent is waiting on a decision from you.
- An activity feed, a phase stepper and overall progress.
- Hover a card to see what it depends on; click it for its full lane history.

## Notes

- Boards bind to `127.0.0.1` only. Ports come from the 8700–8949 range.
- Board state and logs live in `%LOCALAPPDATA%\kitty-dashboard` on Windows and `~/.local/state/kitty-dashboard` elsewhere.
- Missions that route status through a coordination branch are read from that branch when it exists locally.
  Otherwise the board shows the checkout copy and says it may lag behind.
- Developed and tested on Windows. The macOS and Linux paths exist but haven't been exercised yet.
