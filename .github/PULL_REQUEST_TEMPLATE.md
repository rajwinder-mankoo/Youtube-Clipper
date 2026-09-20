## Summary

Describe the problem and the change.

## Verification

- [ ] `python -m compileall -q .`
- [ ] `python -m tests.test_isolation`
- [ ] `node --check dashboard/app.js`
- [ ] Tested with a dry run or private upload when publishing behavior changed

## Safety checklist

- [ ] No OAuth tokens, client secrets, cookies, private media, or generated output are included
- [ ] Uploads still require an explicit approval or deliberate unattended opt-in
- [ ] Metadata remains isolated between unrelated sources
