"""The other half: a search that runs and finds nothing must NOT be an error."""
import sys, json, pathlib
sys.path.insert(0, "src")
from sitefinder import SiteFinder, SearchAttempt
from validate import validate

class Empty:
    class budget:
        winding_down = False
        search_calls = 0
    def get_json(self, *a, **k):
        return {"web": {"results": []}}      # answered, legitimately nothing

a = SiteFinder(Empty(), brave_key="k").search_attempt("Some Tiny AS")
print("status:", a.status, "| error:", repr(a.error), "| failed:", a.failed)
assert a.status == "empty" and a.error == "" and not a.failed
print("PASS: a genuine zero-result answer is not reported as a failure")
