#!/usr/bin/env bash
# Real-browser proof that the live panel updates from the SSE stream, without
# a page reload.
#
# Sequence:
#   A. batch: open /#/live  -> panel attaches to the bridge stream; read it.
#   B. append ONE real change record to runs/changes.jsonl (the bridge tails it).
#   C. batch: read the panel again WITHOUT navigating. A row appearing here can
#      only have arrived over the live stream.
#   D. restore runs/changes.jsonl to 0 lines so no fake data is left behind.
set -u

AB="./node_modules/.bin/agent-browser"
AUD="C:/Users/darak/Downloads/mycompanyhackathonproj/signalpost/runs/audit"
CHANGES="C:/Users/darak/Downloads/mycompanyhackathonproj/signalpost/runs/changes.jsonl"
PY="C:/Users/darak/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"
LOG="$AUD/browser_live_proof.log"
: > "$LOG"

echo "=== A. open console in real Chromium + read panel ===" | tee -a "$LOG"
timeout 100 $AB batch --json < "$AUD/_batch_a.json" > "$AUD/_out_a.json" 2>&1
echo "exit=$?" | tee -a "$LOG"
cat "$AUD/_out_a.json" | tee -a "$LOG"

echo "" | tee -a "$LOG"
echo "=== B. append one real change record ===" | tee -a "$LOG"
"$PY" -c "
import json
from pathlib import Path
rec = {'orgnr':'923609016','field':'legal_name','old':'','new':'EQUINOR ASA',
       'detected_at':'2026-10-10T11:50:00Z','source':'browser-live-proof'}
p = Path(r'$CHANGES')
p.open('a', encoding='utf-8').write(json.dumps(rec) + '\n')
print('appended; lines now =', sum(1 for _ in p.open(encoding='utf-8')))
" | tee -a "$LOG"

sleep 6

echo "" | tee -a "$LOG"
echo "=== C. read panel AGAIN, no navigation / no reload ===" | tee -a "$LOG"
timeout 100 $AB batch --json < "$AUD/_batch_b.json" > "$AUD/_out_b.json" 2>&1
echo "exit=$?" | tee -a "$LOG"
cat "$AUD/_out_b.json" | tee -a "$LOG"

echo "" | tee -a "$LOG"
echo "=== D. restore change log ===" | tee -a "$LOG"
"$PY" -c "from pathlib import Path; Path(r'$CHANGES').write_text('', encoding='utf-8'); print('lines=0 (no fake data left)')" | tee -a "$LOG"

echo "DONE" | tee -a "$LOG"
