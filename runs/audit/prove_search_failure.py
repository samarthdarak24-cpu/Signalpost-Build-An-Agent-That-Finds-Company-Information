"""End-to-end proof: does a failed search reach the published profile?"""
import os, sys
sys.path.insert(0, "src")
os.environ.pop("BRAVE_API_KEY", None)   # the realistic keyless case

from pipeline import process_company, Settings, RunReport
from registry import RegistryStore
from delta import SnapshotStore
from net import HttpClient, Budget
from extract_llm import LlmClient
import threading

print("BRAVE_API_KEY set?", bool(os.environ.get("BRAVE_API_KEY")))

settings = Settings(offline=False, use_llm=False, fetch_web=True)
store = RegistryStore()
snapshots = SnapshotStore()
http = HttpClient(budget=Budget())
res = process_company(
    "923609016", store=store, snapshots=snapshots, settings=settings,
    http=http, llm=LlmClient(), reporter=RunReport(), lock=threading.Lock(),
)
d = res.to_dict()
print("org:      ", d["org_number"], d["legal_name"])
print("search:   ", d["search"])
print("website fact in profile file?")
import json, pathlib as _pl
prof = json.loads(_pl.Path(f"profiles/{d['org_number']}.json").read_text())
fields = [f["field"] for f in prof["facts"]]
print("  fields has website:", "website" in fields)
for s in prof.get("empty_fields", []):
    if s["field"] == "website":
        print("skip why:  ", s["reason"])
print("profile errors:", prof.get("errors"))
print("errors:   ", d["error"])
