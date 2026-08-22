package com.crackedalert.alarm;

import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.TextView;

/**
 * Full-screen alarm screen: shows the alert detail and a big DISMISS button.
 * Dismiss sends ACTION_ACK to the service (which retries POST /ack until it
 * succeeds and keeps ringing meanwhile). The service broadcasts status, so
 * this screen reflects "Acknowledging…" / "Stopped" states and closes itself
 * when the alert clears (e.g. the other phone acked first).
 */
public class AlarmActivity extends Activity {

    private final Handler handler = new Handler(Looper.getMainLooper());
    private TextView titleView;
    private TextView detailView;
    private Button dismissBtn;
    private Button forceBtn;

    private final BroadcastReceiver statusReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            if (!AlarmService.ACTION_STATUS.equals(intent.getAction())) {
                return;
            }
            int st = intent.getIntExtra(AlarmService.EXTRA_STATE, AlarmService.STATE_IDLE);
            if (st == AlarmService.STATE_ACKING) {
                dismissBtn.setText("Acknowledging…");
                dismissBtn.setEnabled(false);
            } else if (st == AlarmService.STATE_IDLE) {
                dismissBtn.setText("Stopped");
                dismissBtn.setEnabled(false);
                handler.postDelayed(new Runnable() {
                    @Override
                    public void run() {
                        finish();
                    }
                }, 1500);
            }
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED
                | WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
                | WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        setContentView(R.layout.activity_alarm);

        titleView = findViewById(R.id.alarm_title);
        detailView = findViewById(R.id.alarm_detail);
        dismissBtn = findViewById(R.id.dismiss_btn);
        forceBtn = findViewById(R.id.force_btn);

        applyExtras(getIntent());

        dismissBtn.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                dismissBtn.setText("Acknowledging…");
                dismissBtn.setEnabled(false);
                startService(new Intent(AlarmActivity.this, AlarmService.class)
                        .setAction(AlarmService.ACTION_ACK));
            }
        });

        forceBtn.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                startService(new Intent(AlarmActivity.this, AlarmService.class)
                        .setAction(AlarmService.ACTION_FORCE_STOP));
                finish();
            }
        });
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        applyExtras(intent);
    }

    private void applyExtras(Intent i) {
        if (i == null) {
            return;
        }
        String d = i.getStringExtra(AlarmService.EXTRA_DETAIL);
        long since = i.getLongExtra(AlarmService.EXTRA_SINCE, 0);
        detailView.setText(d == null || d.isEmpty() ? "An alert is firing." : d);
        if (since > 0) {
            titleView.setText("ALERT ACTIVE · " + AlarmService.timeAgo(since));
        } else {
            titleView.setText("ALERT ACTIVE");
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
    }

    @Override
    protected void onPause() {
        super.onPause();
        try {
            unregisterReceiver(statusReceiver);
        } catch (Exception ignored) {
        }
    }
}
