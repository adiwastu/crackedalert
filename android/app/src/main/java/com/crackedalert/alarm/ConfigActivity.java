package com.crackedalert.alarm;

import android.Manifest;
import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.media.AudioAttributes;
import android.media.MediaPlayer;
import android.media.RingtoneManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.Settings;
import android.view.View;
import android.widget.AdapterView;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.SeekBar;
import android.widget.Spinner;
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
    static final String KEY_SOUND = "sound";      // ringtone uri, "" = default
    static final String KEY_VOLUME = "volume";    // 0..100 multiplier
    static final String KEY_RING_SECS = "ring_secs"; // 0 = until dismissed

    private static final int REQ_PICK_SOUND = 2;

    private EditText urlInput;
    private EditText tokenInput;
    private TextView status;
    private SharedPreferences prefs;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private MediaPlayer testPlayer;

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

        wireSoundControls();
        wireVolumeBar();
        wireRingTimeout();
    }

    private void wireSoundControls() {
        final Button soundBtn = findViewById(R.id.sound_btn);
        soundBtn.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                Intent i = new Intent(RingtoneManager.ACTION_RINGTONE_PICKER);
                i.putExtra(RingtoneManager.EXTRA_RINGTONE_TYPE,
                        RingtoneManager.TYPE_ALARM);
                i.putExtra(RingtoneManager.EXTRA_RINGTONE_SHOW_DEFAULT, true);
                i.putExtra(RingtoneManager.EXTRA_RINGTONE_SHOW_SILENT, false);
                String saved = prefs.getString(KEY_SOUND, "");
                if (!saved.isEmpty()) {
                    i.putExtra(RingtoneManager.EXTRA_RINGTONE_EXISTING_URI,
                            Uri.parse(saved));
                }
                startActivityForResult(i, REQ_PICK_SOUND);
            }
        });
        refreshSoundLabel();

        findViewById(R.id.test_btn).setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                testRing();
            }
        });
    }

    private void refreshSoundLabel() {
        String saved = prefs.getString(KEY_SOUND, "");
        String name = "System default alarm";
        if (!saved.isEmpty()) {
            RingtoneManager rm = new RingtoneManager(this);
            try {
                rm.setType(RingtoneManager.TYPE_ALARM);
                android.media.Ringtone r = rm.getRingtone(this, Uri.parse(saved));
                if (r != null) {
                    name = r.getTitle(this);
                }
            } catch (Exception ignored) {
            }
        }
        ((Button) findViewById(R.id.sound_btn)).setText("Alarm sound: " + name);
    }

    private void testRing() {
        stopTestRing();
        try {
            String saved = prefs.getString(KEY_SOUND, "");
            Uri uri = saved.isEmpty()
                    ? RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
                    : Uri.parse(saved);
            testPlayer = new MediaPlayer();
            testPlayer.setAudioAttributes(new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                    .build());
            testPlayer.setDataSource(this, uri);
            testPlayer.prepare();
            int vol = prefs.getInt(KEY_VOLUME, 100);
            float f = Math.max(0, Math.min(100, vol)) / 100f;
            testPlayer.setVolume(f, f);
            testPlayer.start();
            status.setText("Test ringing for 4s at " + vol
                    + "% — the volume slider on screen while this plays is"
                    + " the phone's Alarm volume.");
            handler.postDelayed(new Runnable() {
                @Override
                public void run() {
                    stopTestRing();
                }
            }, 4000);
        } catch (Exception e) {
            stopTestRing();
            status.setText("Test failed: " + e.getMessage());
        }
    }

    private void stopTestRing() {
        if (testPlayer != null) {
            try {
                testPlayer.stop();
            } catch (Exception ignored) {
            }
            testPlayer.release();
            testPlayer = null;
        }
    }

    private void wireVolumeBar() {
        SeekBar bar = findViewById(R.id.volume_bar);
        bar.setProgress(prefs.getInt(KEY_VOLUME, 100));
        bar.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            @Override
            public void onProgressChanged(SeekBar seekBar, int progress,
                                          boolean fromUser) {
                if (fromUser) {
                    prefs.edit().putInt(KEY_VOLUME, progress).apply();
                }
            }

            @Override
            public void onStartTrackingTouch(SeekBar seekBar) {
            }

            @Override
            public void onStopTrackingTouch(SeekBar seekBar) {
            }
        });
    }

    private void wireRingTimeout() {
        final String[] labels = {"Until dismissed", "After 1 minute",
                "After 2 minutes", "After 5 minutes", "After 10 minutes"};
        final int[] values = {0, 60, 120, 300, 600};
        Spinner spinner = findViewById(R.id.ring_spinner);
        ArrayAdapter<String> adapter = new ArrayAdapter<>(this,
                android.R.layout.simple_spinner_item, labels);
        adapter.setDropDownViewResource(
                android.R.layout.simple_spinner_dropdown_item);
        spinner.setAdapter(adapter);
        int saved = prefs.getInt(KEY_RING_SECS, 300);
        int sel = 3;                       // 5 minutes (the default)
        for (int i = 0; i < values.length; i++) {
            if (values[i] == saved) {
                sel = i;
            }
        }
        spinner.setSelection(sel);
        spinner.setOnItemSelectedListener(new AdapterView.OnItemSelectedListener() {
            @Override
            public void onItemSelected(AdapterView<?> parent, View view,
                                       int position, long id) {
                prefs.edit().putInt(KEY_RING_SECS, values[position]).apply();
            }

            @Override
            public void onNothingSelected(AdapterView<?> parent) {
            }
        });
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode,
                                    Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQ_PICK_SOUND && resultCode == RESULT_OK) {
            Uri picked = data != null
                    ? data.getParcelableExtra(
                          RingtoneManager.EXTRA_RINGTONE_PICKED_URI)
                    : null;
            prefs.edit().putString(KEY_SOUND,
                    picked != null ? picked.toString() : "").apply();
            refreshSoundLabel();
        }
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
        stopTestRing();
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
