package com.crackedalert.alarm;

import android.Manifest;
import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.PowerManager;
import android.provider.Settings;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;

/**
 * Config screen: server URL + alert token, start/stop monitoring, and
 * shortcuts to the Android settings this app needs (notifications,
 * full-screen alerts, battery whitelist).
 */
public class ConfigActivity extends Activity {

    static final String PREFS = "cfg";
    static final String KEY_URL = "url";
    static final String KEY_TOKEN = "token";
    static final String KEY_ENABLED = "enabled";
    static final String KEY_STATUS = "status";

    private EditText urlInput;
    private EditText tokenInput;
    private TextView status;
    private SharedPreferences prefs;

    private final BroadcastReceiver statusReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            if (AlarmService.ACTION_STATUS.equals(intent.getAction())) {
                status.setText(AlarmService.formatStatus(ConfigActivity.this, intent));
            }
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_config);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);

        urlInput = findViewById(R.id.url_input);
        tokenInput = findViewById(R.id.token_input);
        status = findViewById(R.id.status);

        urlInput.setText(prefs.getString(KEY_URL, "https://alert.hotland3x3.my.id"));
        tokenInput.setText(prefs.getString(KEY_TOKEN, ""));

        findViewById(R.id.start_btn).setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                String url = urlInput.getText().toString().trim();
                String token = tokenInput.getText().toString().trim();
                if (url.isEmpty() || token.isEmpty()) {
                    status.setText("Enter the server URL and your alert token first.");
                    return;
                }
                prefs.edit().putString(KEY_URL, url).putString(KEY_TOKEN, token)
                        .putBoolean(KEY_ENABLED, true).apply();
                requestNotificationPermission();
                startService(new Intent(ConfigActivity.this, AlarmService.class)
                        .setAction(AlarmService.ACTION_START));
                status.setText("Starting monitoring…");
            }
        });

        findViewById(R.id.stop_btn).setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                prefs.edit().putBoolean(KEY_ENABLED, false).apply();
                startService(new Intent(ConfigActivity.this, AlarmService.class)
                        .setAction(AlarmService.ACTION_STOP));
                status.setText("Not monitoring.");
            }
        });

        findViewById(R.id.settings_btn).setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                requestNotificationPermission();
                openFullScreenSettings();
                openBatterySettings();
            }
        });
    }

    @Override
    protected void onResume() {
        super.onResume();
        IntentFilter f = new IntentFilter(AlarmService.ACTION_STATUS);
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(statusReceiver, f, Context.RECEIVER_NOT_EXPORTED);
        } else {
            registerReceiver(statusReceiver, f);
        }
        String saved = prefs.getString(KEY_STATUS, "");
        if (!saved.isEmpty()) {
            status.setText(saved);
        } else if (!prefs.getBoolean(KEY_ENABLED, false)) {
            status.setText("Not monitoring.");
        }
    }

    @Override
    protected void onPause() {
        super.onPause();
        try {
            unregisterReceiver(statusReceiver);
        } catch (Exception ignored) {
        }
    }

    private void requestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
    }

    private void openFullScreenSettings() {
        if (Build.VERSION.SDK_INT >= 34) {
            try {
                startActivity(new Intent(Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT,
                        Uri.parse("package:" + getPackageName())));
            } catch (Exception ignored) {
                // Some devices/OEMs lack this settings screen; nothing to do.
            }
        }
    }

    private void openBatterySettings() {
        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        if (pm != null && !pm.isIgnoringBatteryOptimizations(getPackageName())) {
            try {
                startActivity(new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                        Uri.parse("package:" + getPackageName())));
            } catch (Exception ignored) {
            }
        }
    }
}
