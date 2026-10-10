# Signalpost — 100-Company Smoke Test

One complete terminal result for every supplied company, from a fresh
batch file, with no precomputed profiles required.

```
SIGNALPOST SMOKE TEST

Input companies:         100
Output envelopes:        100 OK
Missing outputs:           0 OK
Duplicate outputs:         0 OK

Available:               100
Not available:             0
Ambiguous:                 0
Blocked:                   0
Not applicable:            0
Failed:                    0

Facts extracted:        3074
Evidence-backed:        3074

Runtime:                 310 s
Requests:                206
LLM calls:                 0
Cost:                 $0.0000
```

Every envelope is written to `runs/envelopes.jsonl`, one JSON object per line.
A missing row would make the run unscorable; there are none.
