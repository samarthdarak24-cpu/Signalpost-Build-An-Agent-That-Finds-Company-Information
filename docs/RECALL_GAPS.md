# Signalpost — Recall Gap Report

Computed over 100 profiles. Where the next point of recall is.

```
SIGNALPOST RECALL MATRIX

                         Companies     Facts
Company record               100%       79%
Financials                   100%       98%
People & locations           100%       50%
Website                       70%       20%
Hiring & public activity         2%        1%

Missing high-value fields
  - Website (Website): 81% absent
  - Accountant (People & locations): 100% absent
  - Auditor (People & locations): 100% absent
  - Deputy board chair (People & locations): 100% absent
  - Latest job date (Hiring & public activity): 100% absent
  - Latest update date (Hiring & public activity): 100% absent
  - Owner (People & locations): 100% absent
  - Parent company (Company record): 100% absent
```

## Highest-value missing fields (weight x absence)

| Field | Domain | Absent | Weight |
|---|---|---|---|
| Website | Website | 81% | 1.5 |
| Accountant | People & locations | 100% | 1.0 |
| Auditor | People & locations | 100% | 1.0 |
| Deputy board chair | People & locations | 100% | 1.0 |
| Latest job date | Hiring & public activity | 100% | 1.0 |
| Latest update date | Hiring & public activity | 100% | 1.0 |
| Owner | People & locations | 100% | 1.0 |
| Parent company | Company record | 100% | 1.0 |
| Wikidata | Company record | 100% | 1.0 |
| Careers page | Hiring & public activity | 99% | 1.0 |
| Facebook | Website | 99% | 1.0 |
| Instagram | Website | 99% | 1.0 |
| Latest job title | Hiring & public activity | 99% | 1.0 |
| LEI | Company record | 99% | 1.0 |
| Open roles | Hiring & public activity | 99% | 1.0 |

Priority order follows expected gain: financials, people/roles, locations,
website, jobs, activity.
