package com.crackedalert.alarm;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;

/**
 * Restarts monitoring after a reboot (or app update) if the user had enabled
 * it — a safety alarm must not silently die with the phone.
 */
public class BootReceiver extends BroadcastReceiver {

    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent == null ? "" : intent.getAction();
        if (!Intent.ACTION_BOOT_COMPLETED.equals(action)
                && !Intent.ACTION_MY_PACKAGE_REPLACED.equals(action)) {
            return;
        }
        SharedPreferences p = context.getSharedPreferences(
                ConfigActivity.PREFS, Context.MODE_PRIVATE);
        if (!p.getBoolean(ConfigActivity.KEY_ENABLED, false)) {
            return;
        }
        context.startForegroundService(new Intent(context, AlarmService.class)
                .setAction(AlarmService.ACTION_START));
    }
}
