package app.feedline;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;

/** Starts the notification service again after the phone restarts or the app is updated. */
public class BootReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context c, Intent i) {
        SharedPreferences p = c.getSharedPreferences("feedline", Context.MODE_PRIVATE);
        if (p.getString("url", null) != null && p.getBoolean("notify", true)) {
            try { NotifyService.start(c); } catch (RuntimeException ignored) { /* Android refused a background start: it starts when the app is opened */ }
        }
    }
}
