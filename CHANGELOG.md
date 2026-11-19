# Changelog

## Unreleased

- Tab groups. `[groups]` rules (`name = regex` matched against the tab title or folder) file
  sessions into iTerm2 tab groups. A group collapses once all its tabs are hibernated and expands
  when one wakes (`features.collapse_groups`). The panel and `tabnap top` show group headings, and
  the panel has a "Sleep idle" button per group. `tabnap groups` suggests rules from the folders
  your tabs are in, and `tabnap hibernate --group <name>` puts one group's idle sessions to sleep.
  `tabnap group <name> <tab>...` (and `POST /api/groups`) files tabs into a group by hand.
  Groups get dark, muted tints; set your own with `iterm.group_colors`.
- Tab groups survive a reboot or restore: tabnap remembers each conversation's group and puts its
  new tab back in it. A tab you take out of a group stays out.
- A collapsed group blinks when a tab inside it needs you.
- Panel questions, nothing happens until you answer: "Put this session in <group>?" (opt-in
  `features.group_suggestions`, which asks Claude Haiku to pick the group) and "Still need this tab?"
  for tabs hibernated `stale_days` (7) days, with Keep / Close tab.
- iTerm2: a tab closing mid-scan (`SESSION_NOT_FOUND`) was treated as a lost connection, and the
  reconnect left the old socket open, so it kept the status-bar RPC and every retry failed with
  `DUPLICATE_SERVER_ORIGINATED_RPC` until the daemon was restarted. The scan now skips the closed
  tab, and a reconnect closes the old connection first.
- A terminal that accepts the connection but never answers (iTerm2 mid-restart) no longer freezes the
  daemon: connecting now times out after 20 s and retries.
- iTerm2: reconnect after iTerm2 restarts or updates. The iterm2 library kept the first auth cookie,
  which iTerm2 rejects once used, so the daemon stayed disconnected until restarted.
- iTerm2: a session resumed by hand (not through tabnap) keeps its title.
- iTerm2: a tab woken after a daemon restart could show just "claude" instead of its title. The
  title setting is now restored before the agent starts, so iTerm2's own title bits survive.
- iTerm2: a daemon restart could strip the title of an awake agent tab (it mistook the tab's title
  setting for a hibernated tab's).
- iTerm2: Codex tabs with an empty input box were treated as having a draft, so Sleep all idle and
  auto-hibernate skipped them (iTerm2 reports blank cells as NUL).

## 0.1.0 (2026-11-02)

First release.

- Hibernate, wake and refresh Claude Code and Codex CLI sessions in iTerm2 and tmux.
- Auto-hibernate after N idle minutes, with safety checks (working, waiting, drafts, dialogs,
  background shells, `/loop`, pinned, focused tab).
- "Needs you" pulse, `tabnap top`, iTerm2 Toolbelt panel and status-bar button, browser panel.
- Interactive installer with opt-in features; one-line install script.
