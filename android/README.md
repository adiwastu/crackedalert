# Cracked Alarm (Android)

Ring-until-dismissed alarm app for the [`ALARM_APP.md`](../ALARM_APP.md)
contract. Two phones poll the bot's `alert-status` endpoint; when an alert
fires, both ring a full-screen looping alarm (sound + vibration) until someone
taps DISMISS, which calls `/ack` so both phones stop.

Zero dependencies: plain Java + framework APIs (no AndroidX, no Kotlin, no
WorkManager). The APK is ~1.5 MB.

## What it does

- Polls `GET /alert-status` every ~15 s with `X-Alert-Token` auth
- `active:true` → full-screen alarm screen + looping alarm sound + strong
  vibration; **never auto-stops**
- Network errors keep the previous decision — a ringing alarm stays ringing
- DISMISS → `POST /ack`, retried every 2 s until it succeeds; the ring
  continues while acknowledging
- If the other phone acked first, the next poll sees `inactive` and stops
- Foreground service restarts after reboot if monitoring was enabled
- Status live-updates on the config screen and in the notification

## Requirements (one-time, on the phone)

1. Install the APK (sideload; allow "install unknown apps").
2. Open the app, enter the server URL and `ALERT_STATUS_TOKEN`.
3. Tap **Start monitoring**, then **Grant required permissions**:
   - Notifications (Android 13+)
   - Full-screen alerts ("Alarms & reminders" special access, Android 14+)
   - Battery optimization: **Don't optimize** (keep polling alive)
4. Repeat on the second phone with the same token.

## Build (Windows)

```bat
set JAVA_HOME=C:\Android\jdk-21
gradlew.bat assembleDebug
:: APK: app\build\outputs\apk\debug\app-debug.apk
```

Toolchain used on this machine: JDK 21 (Temurin) at `C:\Android\jdk-21`,
Android SDK at `C:\Android\sdk` (platforms;android-35, build-tools;35.0.0),
Gradle 8.13 + AGP 8.13.2 (cached in `~/.gradle`).

Install over USB:

```bat
adb install -r app\build\outputs\apk\debug\app-debug.apk
```

## Test the ring-until-dismissed loop

1. Start monitoring on both phones (they should show "Monitoring — all clear").
2. Fire a real alert from Telegram (e.g. `/alert <price>` or an SL hit).
3. Both phones should ring full-screen within ~15 s.
4. Tap DISMISS on one — it shows "Acknowledging…" then stops; the other stops
   on its next poll (≤ ~15 s later).
5. Kill the network on one phone while ringing — it must keep ringing.

## Notes

- The token is stored in the app's private SharedPreferences, sent over HTTPS
  only. Anyone with the token can ack alerts; keep it secret.
- `targetSdk 34`: on Android 14+, full-screen intents need the "Alarms &
  reminders" special access (button in the app opens that settings page).
- Release APK: create a keystore (`keytool -genkeypair …`), add a signing
  config in `app/build.gradle`, then `gradlew assembleRelease`.
