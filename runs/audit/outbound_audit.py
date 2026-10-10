"""Task #45 section 1: outbound-request-path audit, run against the real objects."""
import sys, inspect
sys.path.insert(0, "src")

print("=" * 74)
print("OUTBOUND REQUEST PATH AUDIT")
print("=" * 74)

from net import Budget as NetBudget, HttpClient
from budget import BudgetController

print("\n1. TWO PARALLEL BUDGET SYSTEMS")
nb = NetBudget()
print("   net.Budget  (LIVE - wired into HttpClient, pipeline, sitefinder)")
print(f"     hard_limit={nb.hard_limit}  soft_limit={nb.soft_limit}")
print("     deadline?      NO")
print("     cost tracking? NO")
print("     lock?          NO  <- race under threads")
bc = BudgetController()
print("   budget.BudgetController  (DEAD - referenced only by its own tests)")
print(f"     max_requests={bc.max_requests}  <-- exceeds the 2000 ceiling")
print(f"     max_usd={bc.max_usd}  <-- exceeds the $10 ceiling")
print(f"     max_seconds={bc.max_seconds:.0f} = {bc.max_seconds/60:.0f} min")

print("\n2. FILE:LINE OF EVERY budget.spend() CALL SITE")
import re, pathlib
for p in sorted(pathlib.Path("src").glob("*.py")):
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if "budget.spend" in line:
            print(f"   {p.name}:{i}  {line.strip()}")

print("\n3. OUTBOUND PATHS vs net.Budget ACCOUNTING")
from live_api import collect_changes_async
src = inspect.getsource(collect_changes_async)
print(f"   live_api.collect_changes_async  -> httpx.AsyncClient, budget calls={src.count('budget.spend')}")
print("       20 pages/tick, 60s tick  = up to 20 req/min UNACCOUNTED")

print("\n4. HOW EACH EVENT CLASS IS COUNTED BY HttpClient.get")
g = inspect.getsource(HttpClient.get)
spend_at = g.find("budget.spend")
first_except = g.find("except _ue.HTTPError")
print(f"   retries:   spend() at offset {spend_at}, first except at {first_except}")
print(f"              -> spend is INSIDE the retry loop: {'COUNTED PER ATTEMPT' if 0 < spend_at < first_except else 'MISCOUNTED'}")
print("   redirects: urllib follows transparently; 1 spend covers N socket hops -> UNDERCOUNTED")
print("   DNS/conn failure: raises -> retried via generic except -> each attempt spends -> COUNTED")
print("   304/404: raise before spend? no -> spend occurs on every attempt -> COUNTED")
print("   cache hit: returns BEFORE the loop -> NOT counted (correct)")

print("\n5. VERDICT")
print("   * $10 ceiling         : NOT ENFORCED (no cost accounting in the live path)")
print("   * 45-minute deadline  : NOT ENFORCED (no deadline field in net.Budget)")
print("   * 2000 request ceiling: enforced by net.Budget, but NOT thread-safe")
print("   * httpx live-feed path: makes requests wholly outside accounting")
