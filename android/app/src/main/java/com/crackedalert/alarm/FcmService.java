package com.crackedalert.alarm;

import com.google.firebase.messaging.FirebaseMessagingService;
import com.google.firebase.messaging.RemoteMessage;

import android.content.Intent;
import android.os.Build;

import java.util.Map;

/**
 * FCM entry point: the bot's high-priority data message wakes the app
 * here even in Doze (the mechanism polling can never guarantee). The
 * message carries {detail, since}; the full-screen alarm UI itself is
 * AlarmService's job, started immediately. Token registration lives in
 * FcmRegistrar (also run on every config-screen open).
 *
 * Without a google-services.json in the project this class never fires
 * (Firebase never initializes) and the app falls back to pure polling.
 */
public class FcmService extends FirebaseMessagingService {

    @Override
    public void onNewToken(String token) {
        FcmRegistrar.registerToken(this, token);
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
}
