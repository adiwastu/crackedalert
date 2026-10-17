package com.crackedalert.alarm;

import android.content.Context;
import android.content.SharedPreferences;

import com.google.android.gms.tasks.Tasks;
import com.google.firebase.messaging.FirebaseMessaging;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * Registers the device's FCM token with the bot (POST /register-fcm, so
 * the bot knows where to push). Best effort everywhere: without Play
 * services or a Firebase config the getters throw and the app simply
 * falls back to polling.
 *
 * Called from FcmService.onNewToken (Firebase rotated the token) AND on
 * every config-screen open — a token fetched once never fires
 * onNewToken again, so the open-time registration is what self-heals a
 * missed or failed registration (e.g. the endpoint was unreachable
 * during the first launch).
 */
public final class FcmRegistrar {

    private FcmRegistrar() {
    }

    /** Fire-and-forget: fetch the current token in the background and register it. */
    public static void registerAsync(Context context) {
        final Context app = context.getApplicationContext();
        new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    String token = Tasks.await(
                            FirebaseMessaging.getInstance().getToken());
                    if (token != null && !token.isEmpty()) {
                        post(app, token);
                    }
                } catch (Exception ignored) {
                    // No Firebase config / no Play services: polling only.
                }
            }
        }, "fcm-register").start();
    }

    /** From FcmService.onNewToken: Firebase already handed us the token. */
    public static void registerToken(Context context, String token) {
        if (token == null || token.isEmpty()) {
            return;
        }
        final Context app = context.getApplicationContext();
        final String tok = token;
        new Thread(new Runnable() {
            @Override
            public void run() {
                post(app, tok);
            }
        }, "fcm-register").start();
    }

    private static void post(Context context, String deviceToken) {
        SharedPreferences p = context.getSharedPreferences(
                ConfigActivity.PREFS, Context.MODE_PRIVATE);
        String baseUrl = p.getString(ConfigActivity.KEY_URL, "").trim();
        String alertToken = p.getString(ConfigActivity.KEY_TOKEN, "").trim();
        if (baseUrl.isEmpty() || alertToken.isEmpty()) {
            return;
        }
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
            // Retried on the next app open; polling keeps working.
        } finally {
            if (c != null) {
                c.disconnect();
            }
        }
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
