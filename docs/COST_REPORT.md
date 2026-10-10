# Signalpost — Cost & Runtime Report

Measured from a real run (no estimates except the projection line).

```
Test batch              100 companies

Search requests         0
Total requests          206
LLM calls               0
Input tokens            0
Output tokens           0

Elapsed time            5.2 min
Average / company       3.1 sec

Search cost             $0.0000
LLM cost                $0.0000
External API cost       $0.0000
Projected / 1,000       $0.00
```

The registry, financials, GLEIF and Wikidata layers are keyless and free
(NLOD 2.0 / open data). With no LLM and no search the external cost is $0.00;
the projection scales the measured per-profile cost to a 1,000-company batch.
