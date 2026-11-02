# Recording the demo

`docs/demo.gif` is recorded with [VHS](https://github.com/charmbracelet/vhs) from `docs/demo.tape`.

1. Start a tmux server on its own socket (`tmux -L demo`) with the snippet from `tabnap setup tmux`
   and a few windows running Claude Code / Codex, each with one short finished prompt.
2. Run a daemon that only watches that socket:
   `TABNAP_TERMINALS=tmux TABNAP_TMUX_SOCKETS=demo TABNAP_STATE_DIR=/tmp/tabnap-demo tabnap daemon`
3. `vhs docs/demo.tape`

Keep the project path short so the pre-typed resume command fits on one line.
