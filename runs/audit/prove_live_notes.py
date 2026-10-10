"""Confirm the live agent already discloses a throttled search in its notes."""
import sys
sys.path.insert(0, "src")
from liveweb import search_web
out = search_web("AURA REGNSKAP AS")
print("engine:", repr(out.engine), "ok:", out.ok, "n:", len(out.results))
print("error: ", repr(out.error))
print("-> liveagent surfaces this via: answer.notes.append(f'search: {out.error}')")

# Empty-vs-failed distinction in the live stack
print()
print("empty vs failed distinguishable in liveweb? ",
      "error empty when engine answered" if out.results else "error carries the reason")
