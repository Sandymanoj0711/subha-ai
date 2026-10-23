# Subha AI

- `subha_server.py` – backend (deployed on Render via `render.yaml`)
- `mobile/` – Capacitor app (Android APK + iOS IPA built by GitHub Actions)
- `mobile/assets/` – app icon (girl icon). `mobile/scripts/apply_icons.py` writes it into the native projects during the build.

Build: GitHub repo > Actions > "Build Android APK" / "Build iOS IPA (unsigned)" > Run workflow.
Required repo secrets: `SUBHA_APP_URL`, `SUBHA_ACCESS_TOKEN`.
