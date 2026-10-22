# Desktop (Flutter)

Flutter desktop client for the local trial app.

## Requirements

- Flutter 3.22+
- Desktop support enabled (Windows/Linux/macOS)

Enable desktop if needed:

```bash
flutter config --enable-windows-desktop
flutter config --enable-linux-desktop
flutter config --enable-macos-desktop
```

## Install

```bash
cd desktop
flutter pub get
```

## Run

```bash
cd desktop
flutter run -d windows   # or linux / macos
```

## Notes

- The app expects the backend at `http://127.0.0.1:8000`.
- Login seed: admin@local / admin123
- Mock email mode writes `.eml` files to `backend/outbox`.
