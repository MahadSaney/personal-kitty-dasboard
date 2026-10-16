---
description: Open a live kanban board for the Spec Kitty mission in this chat (one board per mission)
---
Open (or stop) the Kitty Board for the mission being worked on in **this chat and this repo**.
The board is a small local server that shows only that mission's work packages and keeps
running until it is stopped. Each mission gets its own board and port, so several can run at once.

Script: `{{SCRIPT}}` (Python stdlib only, read-only).
Every subcommand prints JSON on stdout.

Arguments: `$ARGUMENTS`

## 1. Pick the action

- Empty, or a mission handle → **start**.
- `stop` (optionally followed by a handle) → **stop** that mission's board.
- `stop all` → `stop --all`.
- `list` → `list`.

## 2. Resolve the repo

Use the workspace root of this chat. If the mission work is happening in a different
kittified repo (visible in this chat's commands or paths), use that repo instead.
Paths inside `.worktrees` are fine; the script walks up to the project root.

## 3. Resolve the mission (start / stop)

Use the first that applies, and pass it as `--mission <handle>` (slug, mission_id, or mid8):

1. A handle given in `$ARGUMENTS`.
2. The mission this chat has been driving: look back through this conversation for the
   mission slug or id used with `spec-kitty next`, `spec-kitty agent ...`, `--mission ...`,
   `kitty-specs/<slug>/...` paths, or a `.worktrees/<slug>-...` lane.
3. Otherwise run `{{RUN}} resolve --repo "<repo>"`. If exactly one mission has
   `"active": true`, use it. If none or several are active, ask the user which one
   (offer the top candidates from that list) and do not guess.

## 4. Run it

```{{SHELL}}
{{RUN}} start --repo "<repo>" --mission <handle>
{{RUN}} stop --repo "<repo>" --mission <handle>
{{RUN}} stop --all
{{RUN}} list
```

`start` opens the board in the default browser. If a board for that mission is already
running it is reused (`"reused": true`) rather than started twice.

If the output has `"ok": false`, show the `error` in plain language. When it includes
`candidates`, ask the user to pick one and rerun with `--mission`.

## 5. Report back

Keep it short:
- For start: the board URL as a link, whether it was reused, then one or two lines from
  `summary` (phase, progress %, WPs in flight, and any open decision the agent is waiting on).
  If `summary.source` is `checkout-may-lag`, mention that the board is reading the checkout
  copy because the coordination branch isn't available locally.
- For stop / list: which boards were stopped or are running, with their URLs.
