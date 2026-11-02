from tabnap.agents.claude import Claude
from tabnap.agents.codex import Codex

AGENTS = [Claude(), Codex()]
BY_NAME = {a.name: a for a in AGENTS}


def agent_for(argv):
    return next((a for a in AGENTS if a.matches(argv)), None)


def top_level(table):
    """tty path -> (pid, agent) for the outermost agent process on each terminal."""
    from tabnap.procs import tty_path
    out = {}
    for pid, (ppid, tty, cmd) in table.items():
        path = tty_path(tty)
        if not path:
            continue
        agent = agent_for(cmd.split())
        if not agent:
            continue
        if _under_agent(table, ppid):
            continue  # a subagent, a native binary under its node launcher, or an agent run by another agent
        out[path] = (pid, agent)
    return out


def _under_agent(table, pid):
    for _ in range(64):
        row = table.get(pid)
        if not row:
            return False
        if agent_for(row[2].split()):
            return True
        pid = row[0]
    return False
