# Records docs/demo.gif with VHS (https://github.com/charmbracelet/vhs).
# Needs a tmux server `-L demo` with a few idle Claude Code / Codex sessions, the tabnap status-line
# format from `tabnap setup tmux`, and a tabnap daemon watching it (see docs/demo.md).

Output docs/demo.gif

Set Shell "bash"
Set FontSize 15
Set Width 1400
Set Height 760
Set Padding 14
Set Theme "Catppuccin Mocha"
Set TypingSpeed 70ms

Hide
Type "tmux -L demo attach -t demo:login-test" Enter
Sleep 2s
Show

Sleep 2.5s

# The session list (prefix + N): four agents awake, ~1.2 GB between them
Ctrl+B
Type "N"
Sleep 3.5s

# Put two of them to sleep (the selection follows a session as the list re-sorts)
Down
Sleep 700ms
Type "h"
Sleep 3.5s
Up
Sleep 400ms
Up
Sleep 700ms
Type "h"
Sleep 3.5s

# Jump into a sleeping tab: the agent quit, the resume command is waiting at the prompt
Enter
Sleep 4.5s

# Enter wakes it: same conversation, fresh process
Enter
Sleep 9s

Ctrl+B
Type "N"
Sleep 4s
Type "q"
Sleep 1s
