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
 *   - on active: full-screen looping alarm (sound + vibration), never auto-stops
 *   - keeps ringing on network errors (keeps the previous decision)
 *   - DISMISS -> POST /ack, retried until it succeeds; ring continues meanwhile
 *   - if another phone acked, next poll sees inactive and we stop ringing
 */
public class AlarmService extends Service {

    public static final String ACTION_START = "com.crackedalert.alarm.START";
    public static final String ACTION_STOP = "com.crackedalert.alarm.STOP";
    public static final String ACTION_ACK = "com.crackedalert.alarm.ACK";
    public static final String ACTION_FORCE_STOP = "com.crackedalert.alarm.FORCE_STOP";
    public static final String ACTION_STATUS = "com.crackedalert.alarm.STATUS";

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
            if (worker == null || !worker.isAlive()) {
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
                    if (doAck()) {
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
                if (state != STATE_RINGING) {
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

    /** POST /ack until success. */
    private boolean doAck() {
        try {
            http("/ack", "POST");
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    private String http(String path, String method) throws Exception {
        URL u = new URL(baseUrl + path);
        HttpURLConnection c = (HttpURLConnection) u.openConnection();
        c.setRequestMethod(method);
        c.setConnectTimeout(10_000);
        c.setReadTimeout(10_000);
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
        synchronized (lock) {
            if (state == STATE_ACKING) {
                return; // keep ringing while the ack is still being retried
            }
            state = STATE_IDLE;
        }
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
        getSystemService(NotificationManager.class).cancel(NOTIF_ALARM);
        broadcast();
    }

    private void startPlayer() {
        try {
            Uri uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM);
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
