# Mobile CI/CD Patterns

## iOS Code Signing with Fastlane Match

**The problem:** Certificates expire, provisioning profiles need device UDIDs, teams share creds insecurely.

**The solution:** Fastlane Match stores everything in an encrypted git repo.

### Setup (one time)

```bash
# Install Fastlane
gem install fastlane

# Initialize Match
fastlane match init
# Choose: git repo for storage
# Creates Matchfile

# Generate certificates (development + appstore)
fastlane match development
fastlane match appstore
```

### In CI (GitHub Actions)

```yaml
jobs:
  build-ios:
    runs-on: macos-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
      - uses: ruby/setup-ruby@v1
        with:
          bundler-cache: true
      - name: Install certificates
        run: fastlane match appstore --readonly
        env:
          MATCH_PASSWORD: ${{ secrets.MATCH_PASSWORD }}
          MATCH_GIT_BASIC_AUTHORIZATION: ${{ secrets.MATCH_GIT_AUTH }}
      - name: Build and upload
        run: fastlane ios release
```

### Common iOS Errors

| Error | Fix |
|-------|-----|
| `No signing certificate` | Run `fastlane match appstore` |
| `Profile doesn't include certificate` | Regenerate: `match --force` |
| `ITMS-90035: Invalid signature` | Wrong provisioning profile type |
| `Apple ID disabled` | Wait 24h or use App Store Connect API key |

## Android Signing

```yaml
jobs:
  build-android:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-java@v4
        with:
          distribution: temurin
          java-version: 17
      - name: Decode keystore
        run: echo "${{ secrets.KEYSTORE_BASE64 }}" | base64 -d > android/app/release.keystore
      - name: Build APK
        run: |
          cd android
          ./gradlew assembleRelease
        env:
          KEYSTORE_PASSWORD: ${{ secrets.KEYSTORE_PASSWORD }}
          KEY_ALIAS: ${{ secrets.KEY_ALIAS }}
          KEY_PASSWORD: ${{ secrets.KEY_PASSWORD }}
```

**Critical:** Never lose your upload keystore. Google Play doesn't allow key changes.

## Flutter

```yaml
jobs:
  build-flutter:
    runs-on: macos-latest
    timeout-minutes: 45
    steps:
      - uses: actions/checkout@v4
      - uses: subosito/flutter-action@v2
        with:
          flutter-version: '3.24'
          cache: true
      - run: flutter pub get
      - run: flutter test
      - run: flutter build ios --release --no-codesign
      - run: flutter build appbundle --release
```

## Platform Comparison

| Platform | Best For | macOS Runners | Free Tier |
|----------|----------|---------------|-----------|
| **Codemagic** | Flutter | Included | 500 min/month |
| **Bitrise** | Enterprise | Included | 200 builds/month |
| **GitHub Actions** | Existing GH users | $0.08/min | 2000 min Linux only |
| **Xcode Cloud** | Apple-only shops | Included | 25 hrs/month |

## Speeding Up Mobile Builds

1. **Cache CocoaPods:** `~/.cocoapods` and `Pods/`
2. **Cache Gradle:** `~/.gradle/caches`
3. **Skip code signing in test jobs:** `--no-codesign`
4. **Use M1/M2 runners when available:** 2-3x faster than Intel
