---
name: "CI-CD"
description: "Automate builds, tests, and deployments across web, mobile, and backend applications."
---

## When to Use

Trigger on: automated deployment, continuous integration, pipeline setup, GitHub Actions, GitLab CI, build failing, deploy automatically, CI configuration, release automation.

## Platform Selection

| Stack | Recommended | Why |
|-------|-------------|-----|
| Web (Next.js, Nuxt, static) | Vercel, Netlify | Zero-config, auto-deploys, preview URLs |
| Mobile (iOS/Android/Flutter) | Codemagic, Bitrise + Fastlane | Pre-configured signing, app store upload |
| Backend/Docker | GitHub Actions, GitLab CI | Full control, self-hosted runners option |
| Monorepo | Nx/Turborepo + GHA | Affected detection, build caching |

**Decision tree:** If platform handles deploy automatically (Vercel, Netlify) → skip custom CI. Only add GitHub Actions when you need tests, custom builds, or deploy to your own infra.

## Quick Start Templates

For copy-paste workflows, see `templates.md`.

## Common Pipeline Pitfalls

| Mistake | Impact | Fix |
|---------|--------|-----|
| Using `latest` image tags | Builds break randomly | Pin versions: `node:20.11.0` |
| Not caching dependencies | +5-10 min per build | Cache `node_modules`, `.next/cache` |
| Secrets in workflow files | Leaked in logs/PRs | Use platform secrets, OIDC for cloud |
| Missing `timeout-minutes` | Stuck jobs burn budget | Always set: `timeout-minutes: 15` |
| No `concurrency` control | Redundant runs on rapid pushes | Group by branch/PR |
| Building on every push | Wasted resources | Build on push to main, test on PRs |

## Mobile-Specific: Code Signing

The #1 pain point. iOS requires certificates + provisioning profiles. Android requires keystores.

**The fix:** Use **Fastlane Match** — stores certs/profiles in git repo, syncs across team and CI.

```bash
# One-time setup
fastlane match init
fastlane match appstore

# In CI
fastlane match appstore --readonly
```

For detailed mobile CI/CD patterns (iOS, Android, Flutter), see `mobile.md`.

## Web-Specific: Build Caching

Next.js/Nuxt builds are slow without cache. The `No Cache Detected` warning = full rebuild.

```yaml
# GitHub Actions: persist Next.js cache
- uses: actions/cache@v4
  with:
    path: .next/cache
    key: nextjs-${{ hashFiles('**/package-lock.json') }}
```

For framework-specific configs, see `web.md`.

## Debugging Failed Builds

| Error Pattern | Likely Cause | Check |
|---------------|--------------|-------|
| Works locally, fails in CI | Environment drift | Node version, env vars, OS |
| Intermittent failures | Flaky tests, resource limits | Retry logic, increase timeout |
| `ENOENT` / file not found | Build order, missing artifact | Check `needs:` dependencies |
| Exit code 137 | Out of memory | Use larger runner or optimize |
| Certificate/signing errors | Expired or mismatched creds | Regenerate with Match/Fastlane |

## What This Doesn't Cover

- Container orchestration (Kubernetes) → see `k8s` skill
- Server configuration → see `server` skill
- Monitoring and observability → see `monitoring` skill
