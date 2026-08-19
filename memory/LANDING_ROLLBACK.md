# Landing Page Rollback Playbook

**Last updated**: 2026-07-26
**Current state**: `LandingPageV3` ("Modern Clinical OS" — bone + saffron) live at `/`.
**Legacy state**: v2 landing preserved at `/legacy-landing` (untouched, 76 LOC).

---

## 🔍 Side-by-side compare (safe, no risk)

- **New landing**:    `https://audinexa.com/`
- **Old landing**:    `https://audinexa.com/legacy-landing`

The `/legacy-landing` route works in **preview and production** — same
old design, same old copy, no waitlist link changes. Use it to A/B
before you commit to the flip.

---

## ⏪ Full rollback (if the new one bombs in prod)

**One line change** — swap the import in `/app/frontend/src/App.js`:

```diff
- import LandingPage from './modules/landing/LandingPageV3';
+ import LandingPage from './modules/landing/v2/LandingPage';
```

Then redeploy. `/legacy-landing` continues to work either way.

**No other files need to be reverted.** The v2 component tree
(`/app/frontend/src/modules/landing/v2/*`) and every component it
imports have been left completely untouched. Nothing has been
mutated, moved, or renamed under v2.

---

## 📁 Files that make up each landing

### New landing (LandingPageV3 — active)
- `/app/frontend/src/modules/landing/LandingPageV3.jsx` (918 LOC, single-file)
- Uses shared: `./DiagnosticIllustrations.jsx` (audiogram + tympano illustrations — used by both)
- Fetches: `GET /api/subscription/tiers`, `GET /api/public/live-stats`

### Legacy landing (v2/LandingPage — preserved)
- `/app/frontend/src/modules/landing/v2/LandingPage.jsx` (76 LOC, composition file)
- Composes `./components/Hero.jsx`, `./components/Features.jsx`,
  `./components/Waitlist.jsx`, etc. All still intact under
  `/app/frontend/src/modules/landing/v2/components/`.

---

## 🧪 Verify the rollback works BEFORE production

```bash
# In preview environment:
curl -s https://referral-sprint.preview.emergentagent.com/legacy-landing \
  | grep -oE "(Beta cohort|Join waitlist|Audiology Clinic OS)" | head -3
```

If you see "Beta cohort" or "Join waitlist" in the output, the legacy
landing is live at `/legacy-landing` and rollback is guaranteed to work.

---

## 🗑️ When to permanently remove the v2 tree

Wait ~30 days after the new landing is live in production and you've
seen no rollback need. Then:

1. Delete `/app/frontend/src/modules/landing/v2/` (whole folder)
2. Remove the `import LegacyLandingPage` line from App.js
3. Remove the `/legacy-landing` route from App.js
4. Delete this file

Not urgent. The v2 tree is small (~5 files, ~600 total LOC) and adds
zero runtime cost — nothing loads it unless `/legacy-landing` is
visited.
