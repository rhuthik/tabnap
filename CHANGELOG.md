# Changelog

## Unreleased

- iTerm2: a tab woken after a daemon restart could show just "claude" instead of its title. The
  title setting is now restored before the agent starts, so iTerm2's own title bits survive.
- iTerm2: Codex tabs with an empty input box were treated as having a draft, so Sleep all idle and
  auto-hibernate skipped them (iTerm2 reports blank cells as NUL).

## 0.1.0 (2026-11-02)

First release.

- Hibernate, wake and refresh Claude Code and Codex CLI sessions in iTerm2 and tmux.
- Auto-hibernate after N idle minutes, with safety checks (working, waiting, drafts, dialogs,
  background shells, `/loop`, pinned, focused tab).
- "Needs you" pulse, `tabnap top`, iTerm2 Toolbelt panel and status-bar button, browser panel.
- Interactive installer with opt-in features; one-line install script.
