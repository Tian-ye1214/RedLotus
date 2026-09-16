# Web CI/CD Patterns

## Build Caching

### Next.js

The `.next/cache` directory must persist between builds.

```yaml
# GitHub Actions
- uses: actions/cache@v4
  with:
    path: |
      .next/cache
    key: nextjs-${{ runner.os }}-${{ hashFiles('**/package-lock.json') }}-${{ hashFiles('**/*.js', '**/*.ts', '**/*.tsx') }}
    restore-keys: |
      nextjs-${{ runner.os }}-${{ hashFiles('**/package-lock.json') }}-
      nextjs-${{ runner.os }}-
```

Without this: `No Cache Detected` → full rebuild every time.

### Nuxt

```yaml
- uses: actions/cache@v4
  with:
    path: |
      .nuxt
      .output
      node_modules/.cache
    key: nuxt-${{ hashFiles('**/package-lock.json') }}
```

### Turborepo (Monorepo)

```yaml
- uses: actions/cache@v4
  with:
    path: .turbo
    key: turbo-${{ github.sha }}
    restore-keys: turbo-
```

## Preview Deploys

Most platforms do this automatically:
- **Vercel:** Every PR gets `https://<project>-<hash>.vercel.app`
- **Netlify:** Every PR gets `https://deploy-preview-<n>--<project>.netlify.app`

### Self-hosted preview deploys

Use branch-based subdomains:

```yaml
deploy-preview:
  if: github.event_name == 'pull_request'
  runs-on: ubuntu-latest
  steps:
    - name: Deploy to preview
      run: |
        BRANCH_SLUG=$(echo "${{ github.head_ref }}" | sed 's/[^a-z0-9]/-/g')
        # Deploy to https://${BRANCH_SLUG}.preview.yoursite.com
```

## Environment Variables

| Platform | Where to Set | Secret Support |
|----------|--------------|----------------|
| Vercel | Project Settings → Environment Variables | Yes (encrypted) |
| Netlify | Site Settings → Build & Deploy → Environment | Yes |
| GitHub Actions | Repo Settings → Secrets | Yes |
| GitLab CI | Settings → CI/CD → Variables | Yes (masked) |

**Pattern:** Use different vars per environment:
- `NEXT_PUBLIC_API_URL` → changes per deploy target
- `DATABASE_URL` → different for preview/staging/prod

**Gotcha:** `NEXT_PUBLIC_*` vars are baked into the build. Changing them requires a redeploy.

## Framework-Specific Considerations

| Framework | Self-Host Complexity | Notes |
|-----------|---------------------|-------|
| Next.js | Medium | ISR requires specific infra. App Router needs Node 18+. |
| Nuxt 3 | Low | Nitro compiles to any target. Most portable. |
| SvelteKit | Low | Adapter system. Smallest bundles. |
| Astro | Very Low | Static by default. SSR optional. |
| Remix | Low | Explicit about what runs where. No platform magic. |

## Deploying to VPS

```yaml
deploy:
  runs-on: ubuntu-latest
  steps:
    - uses: actions/checkout@v4
    - run: npm ci && npm run build
    - name: Deploy via SSH
      uses: appleboy/scp-action@v0.1.7
      with:
        host: ${{ secrets.SSH_HOST }}
        username: ${{ secrets.SSH_USER }}
        key: ${{ secrets.SSH_KEY }}
        source: "dist/*"
        target: "/var/www/app"
    - name: Restart service
      uses: appleboy/ssh-action@v1.0.3
      with:
        host: ${{ secrets.SSH_HOST }}
        username: ${{ secrets.SSH_USER }}
        key: ${{ secrets.SSH_KEY }}
        script: sudo systemctl restart myapp
```

## Common Issues

| Problem | Solution |
|---------|----------|
| Build works locally, fails in CI | Check Node version, env vars, OS differences |
| `NEXT_PUBLIC_*` undefined | Variable not set in build environment |
| Preview deploys cost too much | Limit to specific branches, auto-delete after merge |
| Bundle size creeping up | Add size-limit check to CI |
