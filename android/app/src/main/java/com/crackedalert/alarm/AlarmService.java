package com.crackedalert.alarm;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.media.AudioAttributes;
import android.media.MediaPlayer;
import android.media.RingtoneManager;
import android.net.Uri;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import android.os.SystemClock;
import android.os.VibrationEffect;
import android.os.Vibrator;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * Foreground service implementing the ALARM_APP.md contract:
 *   - polls GET /alert-status every ~15 s (X-Alert-Token auth)
 *   - on FCM push (primary trigger) or an active poll: full-screen
 *     looping alarm (sound + vibration)
 *   - rings at most ring_secs (user setting; 0 = until dismissed), then
 *     quiets for THAT alert (snooze) -- a newer alert re-rings
 *   - keeps ringing on network errors (keeps the previous decision)
 *   - DISMISS -> POST /ack, retried until it succeeds
 *   - if another phone acked, next poll sees inactive and we stop
 */
public class AlarmService extends Service {

    public static final String ACTION_START = "com.crackedalert.alarm.START";
    public static final String ACTION_STOP = "com.crackedalert.alarm.STOP";
    public static final String ACTION_ACK = "com.crackedalert.alarm.ACK";
    public static final String ACTION_FORCE_STOP = "com.crackedalert.alarm.FORCE_STOP";
    public static final String ACTION_STATUS = "com.crackedalert.alarm.STATUS";
    public static final String ACTION_PUSH = "com.crackedalert.alarm.PUSH";

    public static final String EXTRA_STATE = "state";
    public static final String EXTRA_DETAIL = "detail";
    public static final String EXTRA_SINCE = "since";
    public static final String EXTRA_LAST_POLL = "lastPoll";
    public static final String EXTRA_ERROR = "error";

    static final int STATE_IDLE = 0;
    static final int STATE_RINGING = 1;
    static final int STATE_ACKING = 2;

    private static final int NOTIF_MONITOR = 1;
    private static final int NOTIF_ALARM = 2;
    private static final String CH_MONITOR = "monitor";
    private static final String CH_ALARM = "alarm";
    private static final long POLL_MS = 15_000;
    private static final long ACK_RETRY_MS = 2_000;

    // Sleep window on the DEVICE's own clock (the operator's time).
    // Alerts that arrive inside it are snoozed, never rung, and are not
    // caught up after the window ends -- the alert still shows in
    // Telegram. 22:00 inclusive to 05:00 exclusive.
    private static final int QUIET_FROM_HOUR = 22;
    private static final int QUIET_TO_HOUR = 5;

    private final Object lock = new Object();
    private Thread worker;
    private volatile boolean running = false;
    private volatile int state = STATE_IDLE;
    private volatile String detail = "";
    private volatile long since = 0;
    private volatile long lastPoll = 0;
    private volatile String lastError = "";

    private MediaPlayer player;
    private Vibrator vibrator;
    private PowerManager.WakeLock ringLock;
    private volatile long ringStartedAt = 0;   // elapsedRealtime ms
    private volatile long snoozedSince = -1;   // alert we timed out on
    private String baseUrl = "";
    private String token = "";

    @Override
    public void onCreate() {
        super.onCreate();
        vibrator = (Vibrator) getSystemService(VIBRATOR_SERVICE);
        createChannels();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent != null ? intent.getAction() : ACTION_START;
        SharedPreferences p = getSharedPreferences(ConfigActivity.PREFS, MODE_PRIVATE);
        baseUrl = p.getString(ConfigActivity.KEY_URL, "").trim();
        token = p.getString(ConfigActivity.KEY_TOKEN, "").trim();

        if (ACTION_STOP.equals(action) || ACTION_FORCE_STOP.equals(action)) {
            running = false;
            if (worker != null) {
                worker.interrupt();
            }
            synchronized (lock) {
                state = STATE_IDLE; // force-stop must kill the ringing even mid-ack
            }
            stopRinging();
            stopForeground(true);
            stopSelf();
            return START_NOT_STICKY;
        }
        if (ACTION_ACK.equals(action)) {
            startAcking();
            return START_STICKY;
        }
        if (ACTION_PUSH.equals(action)) {
            // FCM data message: the authoritative "an alert is live"
            // signal. Wake the poll loop (the process may have been
            // dead) and ring immediately instead of waiting up to a
            // poll interval.
            String d = intent.getStringExtra(EXTRA_DETAIL);
            long s = intent.getLongExtra(EXTRA_SINCE, 0);
            startMonitoring();
            if (s > 0 && s != snoozedSince) {
                detail = d == null ? "" : d;
                since = s;
                if (isQuietNow()) {
                    snoozedSince = since;
                    lastError = "quiet hours \u2014 ring suppressed";
                } else if (state != STATE_RINGING) {
                    startRinging();
                }
            }
            return START_STICKY;
        }
        if (baseUrl.isEmpty() || token.isEmpty()) {
            stopSelf();
            return START_NOT_STICKY;
        }
        startMonitoring();
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        running = false;
        if (worker != null) {
            worker.interrupt();
        }
        stopRinging();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void startMonitoring() {
        startForeground(NOTIF_MONITOR, monitorNotification("Monitoring alerts"));
        synchronized (lock) {
            // The old isAlive()-only check raced: a START arriving while
            // a STOP was still tearing the old worker down (isAlive()
            // true for a few ms after running=false) skipped the spawn,
            // leaving a live service with NO poll loop until reboot.
            if (worker == null || !worker.isAlive() || !running) {
                worker = new Thread(new Runnable() {
                    @Override
                    public void run() {
                        workerLoop();
                    }
                }, "alarm-poller");
                worker.start();
            }
        }
    }

    private void startAcking() {
        if (state == STATE_RINGING) {
            synchronized (lock) {
                state = STATE_ACKING;
            }
            broadcast();
        }
    }

    private void workerLoop() {
        running = true;
        while (running) {
            boolean acking = state == STATE_ACKING;
            try {
                if (acking) {
                    // Ack, then confirm the server really cleared before
                    // going silent -- a 200 that raced/lost would otherwise
                    // leave the alarm re-armed on the next poll.
                    if (doAck() && serverIsClear()) {
                        synchronized (lock) {
                            state = STATE_IDLE;
                        }
                        stopRinging();
                        lastError = "";
                        broadcast();
                    } else {
                        lastError = "ack failed, retrying…";
                        broadcast();
                    }
                } else {
                    pollOnce();
                    checkRingTimeout();
                }
            } catch (Throwable t) {
                lastError = String.valueOf(t.getMessage());
                broadcast();
            }
            try {
                Thread.sleep(acking ? ACK_RETRY_MS : POLL_MS);
            } catch (InterruptedException e) {
                break;
            }
        }
    }

    /**
     * Ring timeout: after ring_secs (0 = never) the alarm quiets itself
     * and snoozes THAT alert (matched by its `since`), so a later poll
     * of the same alert does not re-ring. A NEW alert (new since) rings
     * normally. This is the backstop for "it rang forever with no way
     * to stop it" -- even with no notification and no UI, it stops.
     */
    private void checkRingTimeout() {
        if (state != STATE_RINGING || ringStartedAt == 0) {
            return;
        }
        long secs = getSharedPreferences(ConfigActivity.PREFS, MODE_PRIVATE)
                .getInt(ConfigActivity.KEY_RING_SECS, 300);
        if (secs <= 0) {
            return;
        }
        if (SystemClock.elapsedRealtime() - ringStartedAt < secs * 1000L) {
            return;
        }
        snoozedSince = since;
        synchronized (lock) {
            state = STATE_IDLE;
        }
        stopRinging();
        broadcast();
    }

    /** One GET /alert-status. Keeps the previous decision on any failure. */
    private void pollOnce() {
        String body;
        try {
            body = http("/alert-status", "GET");
        } catch (HttpException e) {
            if (e.code == 401) {
                lastError = "unauthorized — check your token";
            } else {
                lastError = e.getMessage();
            }
            lastPoll = System.currentTimeMillis();
            broadcast();
            return;
        } catch (Exception e) {
            lastError = e.getMessage() == null ? "network error" : e.getMessage();
            lastPoll = System.currentTimeMillis();
            broadcast();
            return;
        }
        lastPoll = System.currentTimeMillis();
        lastError = "";
        try {
            JSONObject o = new JSONObject(body);
            boolean active = o.optBoolean("active", false);
            if (active) {
                detail = o.optString("detail", "");
                since = o.optLong("since", 0);
                if (state == STATE_RINGING) {
                    // Old bug: one failed startPlayer() left state=RINGING
                    // with no sound forever. Retry the sound on every
                    // poll that still sees the alert active.
                    ensureSound();
                } else if (since == snoozedSince) {
                    // Timed out or quiet-suppressed earlier: stays quiet.
                } else if (isQuietNow()) {
                    snoozedSince = since;
                    lastError = "quiet hours \u2014 ring suppressed";
                } else {
                    startRinging();
                }
            } else if (state == STATE_RINGING) {
                // Acknowledged elsewhere; the other phone already acked.
                synchronized (lock) {
                    state = STATE_IDLE;
                }
                stopRinging();
            }
        } catch (Exception e) {
            lastError = "bad response: " + e.getMessage();
        }
        broadcast();
    }

    /** True inside the sleep window (22:00..04:59 device-local). */
    static boolean isQuietNow() {
        java.util.Calendar c = java.util.Calendar.getInstance();
        int hour = c.get(java.util.Calendar.HOUR_OF_DAY);
        return hour >= QUIET_FROM_HOUR || hour < QUIET_TO_HOUR;
    }

    /** POST /ack until success. */
    private boolean doAck() {
        try {
            http("/ack?since=" + since, "POST", 5_000, 5_000);
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    /**
     * Confirm the server actually cleared the alert. The worker calls this
     * right after a successful ack; a 200 that was lost/raced on the wire
     * would otherwise leave the alarm re-armed on the next poll.
     */
    private boolean serverIsClear() {
        try {
            String body = http("/alert-status", "GET", 5_000, 5_000);
            return !new JSONObject(body).optBoolean("active", false);
        } catch (Exception e) {
            return false;
        }
    }

    private String http(String path, String method) throws Exception {
        return http(path, method, 10_000, 10_000);
    }

    private String http(String path, String method,
                        int connectMs, int readMs) throws Exception {
        URL u = new URL(baseUrl + path);
        HttpURLConnection c = (HttpURLConnection) u.openConnection();
        c.setRequestMethod(method);
        c.setConnectTimeout(connectMs);
        c.setReadTimeout(readMs);
        c.setRequestProperty("X-Alert-Token", token);
        c.setRequestProperty("User-Agent", "CrackedAlarm/1.0");
        if ("POST".equals(method)) {
            c.setDoOutput(true);
            OutputStream os = c.getOutputStream();
            os.close();
        }
        int code = c.getResponseCode();
        InputStream in = code >= 400 ? c.getErrorStream() : c.getInputStream();
        String body = readAll(in);
        if (code == 401) {
            throw new HttpException("unauthorized", 401);
        }
        if (code != 200) {
            throw new HttpException("HTTP " + code, code);
        }
        return body;
    }

    private static String readAll(InputStream in) throws IOException {
        if (in == null) {
            return "";
        }
        BufferedReader r = new BufferedReader(
                new InputStreamReader(in, StandardCharsets.UTF_8));
        StringBuilder sb = new StringBuilder();
        String line;
        while ((line = r.readLine()) != null) {
            sb.append(line);
        }
        return sb.toString();
    }

    private void startRinging() {
        synchronized (lock) {
            if (state == STATE_RINGING) {
                return;
            }
            state = STATE_RINGING;
        }
        ringStartedAt = SystemClock.elapsedRealtime();
        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        if (pm != null) {
            ringLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "crackedalarm:ring");
            ringLock.acquire();
        }
        startPlayer();
        if (vibrator != null && vibrator.hasVibrator()) {
            if (Build.VERSION.SDK_INT >= 26) {
                vibrator.vibrate(VibrationEffect.createWaveform(
                        new long[]{0, 1200, 500}, 0));
            }
        }
        postAlarmNotification();
        launchAlarmScreen();
        broadcast();
    }

    private void stopRinging() {
        // Always tear down the alarm hardware. The old ACKING guard made
        // stopRinging() a no-op while an ack was retrying -- and since
        // onDestroy() also calls this, a service killed mid-ack would leave
        // the looping player + wake lock running forever with no worker
        // thread to stop it ("the alarm won't go away").
        if (player != null) {
            try {
                player.stop();
            } catch (Exception ignored) {
            }
            player.release();
            player = null;
        }
        if (vibrator != null) {
            vibrator.cancel();
        }
        if (ringLock != null && ringLock.isHeld()) {
            ringLock.release();
            ringLock = null;
        }
        ringStartedAt = 0;
        getSystemService(NotificationManager.class).cancel(NOTIF_ALARM);
        synchronized (lock) {
            if (state == STATE_ACKING) {
                // Explicit stop (FORCE_STOP / onDestroy) already handled
                // above; leave ACKING so the worker can finish its retry.
                // The alarm hardware is silent regardless.
            } else {
                state = STATE_IDLE;
            }
        }
        broadcast();
    }

    private void ensureSound() {
        // The player can die (transient media error); while the alert is
        // still active, every poll retries it instead of staying silent.
        if (player == null) {
            startPlayer();
        }
    }

    private void startPlayer() {
        try {
            SharedPreferences p = getSharedPreferences(
                    ConfigActivity.PREFS, MODE_PRIVATE);
            Uri uri = null;
            String sound = p.getString(ConfigActivity.KEY_SOUND, "").trim();
            if (!sound.isEmpty()) {
                try {
                    uri = Uri.parse(sound);
                } catch (Exception ignored) {
                }
            }
            if (uri == null) {
                uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM);
            }
            if (uri == null) {
                uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE);
            }
            player = new MediaPlayer();
            player.setAudioAttributes(new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                    .build());
            player.setDataSource(this, uri);
            player.setLooping(true);
            player.prepare();
            // Multiplier over the system's Alarm volume (0..100).
            int vol = p.getInt(ConfigActivity.KEY_VOLUME, 100);
            float f = Math.max(0, Math.min(100, vol)) / 100f;
            player.setVolume(f, f);
            player.start();
        } catch (Exception e) {
            lastError = "sound failed: " + e.getMessage();
            player = null;
        }
    }

    private void postAlarmNotification() {
        Intent i = new Intent(this, AlarmActivity.class);
        i.putExtra(EXTRA_DETAIL, detail);
        i.putExtra(EXTRA_SINCE, since);
        PendingIntent pi = PendingIntent.getActivity(this, 2, i,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        // Stop lives ON the notification: even if the full-screen
        // activity is gone (or the OEM lets this be swiped away), there
        // is always a one-tap way to kill the ringing. The ring timeout
        // backstops everything.
        Intent stop = new Intent(this, AlarmService.class)
                .setAction(ACTION_FORCE_STOP);
        PendingIntent stopPi = PendingIntent.getService(this, 3, stop,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification n = new Notification.Builder(this, CH_ALARM)
                .setSmallIcon(R.drawable.ic_stat)
                .setContentTitle("ALERT ACTIVE")
                .setContentText(detail.isEmpty() ? "An alert is firing" : detail)
                .setCategory(Notification.CATEGORY_ALARM)
                .setFullScreenIntent(pi, true)
                .setOngoing(true)
                .setVisibility(Notification.VISIBILITY_PUBLIC)
                .setAutoCancel(false)
                .setContentIntent(pi)
                .addAction(new Notification.Action(R.drawable.ic_stat,
                        "Stop", stopPi))
                .build();
        getSystemService(NotificationManager.class).notify(NOTIF_ALARM, n);
    }

    private void launchAlarmScreen() {
        try {
            Intent i = new Intent(this, AlarmActivity.class)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
            i.putExtra(EXTRA_DETAIL, detail);
            i.putExtra(EXTRA_SINCE, since);
            startActivity(i);
        } catch (Exception ignored) {
            // Full-screen intent notification is the fallback.
        }
    }

    private Notification monitorNotification(String text) {
        Intent i = new Intent(this, ConfigActivity.class);
        PendingIntent pi = PendingIntent.getActivity(this, 1, i,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, CH_MONITOR)
                .setSmallIcon(R.drawable.ic_stat)
                .setContentTitle("Cracked Alarm")
                .setContentText(text)
                .setOngoing(true)
                .setContentIntent(pi)
                .build();
    }

    private void createChannels() {
        NotificationManager nm = getSystemService(NotificationManager.class);
        NotificationChannel mon = new NotificationChannel(
                CH_MONITOR, "Monitoring status", NotificationManager.IMPORTANCE_LOW);
        mon.setDescription("Ongoing status while monitoring");
        nm.createNotificationChannel(mon);
        NotificationChannel al = new NotificationChannel(
                CH_ALARM, "Alarm", NotificationManager.IMPORTANCE_HIGH);
        al.setDescription("Full-screen alert when an alarm fires");
        al.setSound(null, null);
        al.enableVibration(false);
        al.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
        nm.createNotificationChannel(al);
    }

    private void broadcast() {
        Intent i = new Intent(ACTION_STATUS);
        i.putExtra(EXTRA_STATE, state);
        i.putExtra(EXTRA_DETAIL, detail);
        i.putExtra(EXTRA_SINCE, since);
        i.putExtra(EXTRA_LAST_POLL, lastPoll);
        i.putExtra(EXTRA_ERROR, lastError);
        sendBroadcast(i);
        getSharedPreferences(ConfigActivity.PREFS, MODE_PRIVATE)
                .edit()
                .putString(ConfigActivity.KEY_STATUS, formatStatus(this, i))
                .apply();
    }

    static String formatStatus(Context context, Intent i) {
        int st = i.getIntExtra(EXTRA_STATE, STATE_IDLE);
        String d = i.getStringExtra(EXTRA_DETAIL);
        String err = i.getStringExtra(EXTRA_ERROR);
        long poll = i.getLongExtra(EXTRA_LAST_POLL, 0);
        StringBuilder sb = new StringBuilder();
        if (st == STATE_RINGING) {
            sb.append("RINGING — ").append(d == null || d.isEmpty() ? "alert active" : d);
        } else if (st == STATE_ACKING) {
            sb.append("Dismissing… (acknowledging)");
        } else {
            sb.append("Monitoring — all clear");
        }
        if (err != null && !err.isEmpty()) {
            sb.append("\nNote: ").append(err);
        }
        if (poll > 0) {
            sb.append("\nLast check: ").append(timeAgo(poll / 1000));
        }
        return sb.toString();
    }

    static String timeAgo(long unixSecs) {
        long diff = (System.currentTimeMillis() / 1000) - unixSecs;
        if (diff < 60) {
            return "just now";
        }
        long m = diff / 60;
        if (m < 60) {
            return m + " min ago";
        }
        return (m / 60) + "h " + (m % 60) + "m ago";
    }

    private static class HttpException extends Exception {
        final int code;

        HttpException(String message, int code) {
            super(message);
            this.code = code;
        }
    }
}
