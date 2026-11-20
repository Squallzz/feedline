package app.feedline;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.net.Uri;
import android.os.Build;
import android.os.IBinder;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * Shows a notification when a print finishes, fails, is cancelled or pauses.
 * Keeps one long-poll request open to the Feedline server (GET /api/events); no Google services or third party are involved.
 */
public class NotifyService extends Service {
    static final String CH_WATCH = "watch", CH_PRINTS = "prints";
    private volatile boolean running;
    private Thread worker;

    static void start(Context c) {
        Intent i = new Intent(c, NotifyService.class);
        if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i); else c.startService(i);
    }

    @Override public IBinder onBind(Intent i) { return null; }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        channels();
        Notification n = base(CH_WATCH).setSmallIcon(icon()).setContentTitle("Feedline")
            .setContentText("Watching your printers").setOngoing(true).setContentIntent(open(null)).build();
        if (Build.VERSION.SDK_INT >= 34) startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        else startForeground(1, n);
        if (worker == null || !worker.isAlive()) {
            running = true;
            worker = new Thread(this::loop, "feedline-events");
            worker.start();
        }
        return START_STICKY;
    }

    @Override public void onDestroy() {
        running = false;
        if (worker != null) worker.interrupt();
        super.onDestroy();
    }

    private SharedPreferences prefs() { return getSharedPreferences("feedline", MODE_PRIVATE); }

    private void loop() {
        long backoff = 5000;
        while (running) {
            SharedPreferences p = prefs();
            String saved = p.getString("url", null);
            if (saved == null || !p.getBoolean("notify", true)) { stopSelf(); return; }
            long t0 = System.currentTimeMillis();
            try {
                Uri b = Uri.parse(saved);
                long last = p.getLong("last", -1);
                String u = b.getScheme() + "://" + b.getEncodedAuthority() + "/api/events?wait=25" + (last >= 0 ? "&since=" + last : "");
                JSONObject j = new JSONObject(get(u));
                JSONArray ev = j.getJSONArray("events");
                for (int k = 0; k < ev.length(); k++) show(ev.getJSONObject(k));
                p.edit().putLong("last", j.getLong("last")).apply();
                backoff = 5000;
                if (ev.length() == 0 && System.currentTimeMillis() - t0 < 1000) sleep(2000);      // the first call answers at once; do not spin
            } catch (InterruptedException e) {
                return;
            } catch (Exception e) {                                    // server or network away: try again, slower each time (max 60 s)
                try { sleep(backoff); } catch (InterruptedException ie) { return; }
                backoff = Math.min(60000, backoff * 2);
            }
        }
    }

    private static void sleep(long ms) throws InterruptedException { Thread.sleep(ms); }

    private static String get(String url) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(8000);
        c.setReadTimeout(40000);
        try (InputStream in = c.getInputStream()) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            for (int n; (n = in.read(buf)) > 0; ) out.write(buf, 0, n);
            return out.toString("UTF-8");
        } finally { c.disconnect(); }
    }

    private void show(JSONObject ev) {
        String name = ev.optString("name", ""), title = ev.optString("title", "Print update");
        Notification n = base(CH_PRINTS).setSmallIcon(icon())
            .setContentTitle(name.isEmpty() ? title : name + " - " + title).setContentText(ev.optString("text", ""))
            .setStyle(new Notification.BigTextStyle().bigText(ev.optString("text", "")))
            .setAutoCancel(true).setContentIntent(open(ev.optString("printer", null))).build();
        ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).notify((int) (ev.optLong("id") % Integer.MAX_VALUE), n);
    }

    private Notification.Builder base(String channel) {
        if (Build.VERSION.SDK_INT >= 26) return new Notification.Builder(this, channel);
        Notification.Builder b = new Notification.Builder(this);
        if (CH_PRINTS.equals(channel)) b.setPriority(Notification.PRIORITY_HIGH).setDefaults(Notification.DEFAULT_ALL);
        else b.setPriority(Notification.PRIORITY_MIN);
        return b;
    }

    private int icon() { return getResources().getIdentifier("ic_stat", "drawable", getPackageName()); }

    private PendingIntent open(String printer) {
        Intent i = new Intent(this, MainActivity.class).setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        if (printer != null) i.putExtra("printer", printer);
        return PendingIntent.getActivity(this, printer == null ? 0 : printer.hashCode(), i, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    private void channels() {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager m = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        m.createNotificationChannel(new NotificationChannel(CH_PRINTS, "Print events", NotificationManager.IMPORTANCE_HIGH));
        m.createNotificationChannel(new NotificationChannel(CH_WATCH, "Background connection", NotificationManager.IMPORTANCE_MIN));
    }
}
