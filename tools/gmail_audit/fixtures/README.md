# Gmail Audit Fixtures

This fixture pack is intentionally lightweight and shadow-oriented.

What it covers:
- deterministic preclassification lanes
- stage contract validation
- case-link heuristics
- business/action/reply contract stability
- preview metadata consistency

What it does not claim:
- live Gmail validation
- live Groq validation
- live Daszek runtime validation

Run it with:

```powershell
python tools/gmail_audit/verify_fixtures.py
```
