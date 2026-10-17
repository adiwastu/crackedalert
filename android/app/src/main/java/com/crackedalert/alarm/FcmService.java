package com.crackedalert.alarm;

import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;

import com.google.firebase.messaging.FirebaseMessagingService;
import com.google.firebase.messaging.RemoteMessage;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.Map;

/**
 * FCM entry point: the bot's high-priority data message wakes the app
 * here even in Doze (the mechanism polling can never guarantee). The
 * message carries {detail, since}; the full-screen alarm UI itself is
 * AlarmService's job, started immediately. Also keeps the bot's device
 * registry current when Firebase rotates the token.
 *
 * Without a google-services.json in the project this class never fires
 * (Firebase never initializes) and the app falls back to pure polling.
 */
public class FcmService extends FirebaseMessagingService {

    @Override
    public void onNewToken(String token) {
        register(token);
    }

    @Override
    public void onMessageReceived(RemoteMessage message) {
        Map<String, String> data = message.getData();
        Intent i = new Intent(this, AlarmService.class);
        i.setAction(AlarmService.ACTION_PUSH);
        if (data != null) {
            String detail = data.get("detail");
            if (detail != null) {
                i.putExtra(AlarmService.EXTRA_DETAIL, detail);
            }
            String since = data.get("since");
            if (since != null) {
                try {
                    i.putExtra(AlarmService.EXTRA_SINCE,
                            Long.parseLong(since));
                } catch (NumberFormatException ignored) {
                }
            }
        }
        // A high-priority FCM delivery grants the temporary background
        // exemption that makes this foreground-service start legal.
        if (Build.VERSION.SDK_INT >= 26) {
            startForegroundService(i);
        } else {
            startService(i);
        }
    }

    /** POST /register-fcm so the bot knows where to push. Best effort. */
    private void register(String deviceToken) {
        SharedPreferences p = getSharedPreferences(
                ConfigActivity.PREFS, MODE_PRIVATE);
        final String baseUrl = p.getString(ConfigActivity.KEY_URL, "").trim();
        final String alertToken = p.getString(ConfigActivity.KEY_TOKEN, "").trim();
        if (baseUrl.isEmpty() || alertToken.isEmpty()) {
            return;
        }
        new Thread(new Runnable() {
            @Override
            public void run() {
                HttpURLConnection c = null;
                try {
                    c = (HttpURLConnection) new URL(
                            baseUrl + "/register-fcm").openConnection();
                    c.setRequestMethod("POST");
                    c.setConnectTimeout(10_000);
                    c.setReadTimeout(10_000);
                    c.setRequestProperty("X-Alert-Token", alertToken);
                    c.setRequestProperty("Content-Type", "application/json");
                    c.setDoOutput(true);
                    byte[] body = jsonBody(deviceToken)
                            .getBytes(StandardCharsets.UTF_8);
                    c.setFixedLengthStreamingMode(body.length);
                    OutputStream os = c.getOutputStream();
                    os.write(body);
                    os.close();
                    InputStream in = c.getResponseCode() >= 400
                            ? c.getErrorStream() : c.getInputStream();
                    if (in != null) {
                        in.close();
                    }
                } catch (Exception ignored) {
                    // The app re-registers whenever Firebase rotates the
                    // token; a missed attempt is retried by that cycle.
                } finally {
                    if (c != null) {
                        c.disconnect();
                    }
                }
            }
        }, "fcm-register").start();
    }

    private static String jsonBody(String deviceToken) {
        StringBuilder sb = new StringBuilder("{\"token\":\"");
        for (int i = 0; i < deviceToken.length(); i++) {
            char ch = deviceToken.charAt(i);
            if (ch == '"' || ch == '\\') {
                sb.append('\\');
            }
            sb.append(ch);
        }
        return sb.append("\"}").toString();
    }
}
