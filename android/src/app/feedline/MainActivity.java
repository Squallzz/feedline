package app.feedline;

import android.Manifest;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.PowerManager;
import android.provider.Settings;
import android.webkit.JavascriptInterface;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.os.Environment;
import android.text.InputType;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.JsResult;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.Toast;

/** Feedline: a full-screen WebView around your Feedline server. The address is asked for on first start. */
public class MainActivity extends Activity {
    static final String EXAMPLE_URL = "http://192.168.1.60:8081/";   // only shown as a hint; the address is always asked for
    static final String ACTION_CHANGE = "app.feedline.CHANGE_ADDRESS";
    private WebView web;
    private SharedPreferences prefs;
    private ValueCallback<Uri[]> chooser;
    private boolean failed;
    private static final int REQ_FILE = 7;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        prefs = getSharedPreferences("feedline", MODE_PRIVATE);
        getWindow().setStatusBarColor(Color.parseColor("#0A0814"));
        getWindow().setNavigationBarColor(Color.parseColor("#0A0814"));
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);   // keep the screen on while watching a print
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor("#0A0814"));
        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#0A0814"));
        root.addView(web, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);   // printer camera / touchscreen are plain http
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, true);

        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) { return false; }   // stay inside the app
            @Override public void onPageStarted(WebView v, String url, android.graphics.Bitmap icon) { failed = false; }
            @Override public void onPageFinished(WebView v, String url) {      // a page that loaded clears an old error dialog
                if (!failed && errDialog != null) { errDialog.dismiss(); errDialog = null; }
            }
            @Override public void onReceivedError(WebView v, WebResourceRequest r, WebResourceError e) {
                if (r.isForMainFrame()) { failed = true; showError(e.getDescription().toString()); }
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override public boolean onJsAlert(WebView v, String url, String msg, final JsResult r) {
                new AlertDialog.Builder(MainActivity.this).setMessage(msg).setOnCancelListener(d -> r.cancel())
                    .setPositiveButton("OK", (d, w) -> r.confirm()).show();
                return true;
            }
            @Override public boolean onJsConfirm(WebView v, String url, String msg, final JsResult r) {
                new AlertDialog.Builder(MainActivity.this).setMessage(msg).setOnCancelListener(d -> r.cancel())
                    .setPositiveButton("OK", (d, w) -> r.confirm()).setNegativeButton("Cancel", (d, w) -> r.cancel()).show();
                return true;
            }
            @Override public boolean onShowFileChooser(WebView v, ValueCallback<Uri[]> cb, FileChooserParams p) {
                if (chooser != null) chooser.onReceiveValue(null);
                chooser = cb;
                try { startActivityForResult(p.createIntent(), REQ_FILE); }
                catch (Exception e) { chooser = null; return false; }
                return true;
            }
        });
        web.setDownloadListener(new DownloadListener() {         // log export and other file downloads
            @Override public void onDownloadStart(String url, String ua, String cd, String mime, long len) {
                try {
                    DownloadManager.Request q = new DownloadManager.Request(Uri.parse(url));
                    q.setMimeType(mime);
                    q.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                    q.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, URLUtil.guessFileName(url, cd, mime));
                    ((DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE)).enqueue(q);
                    Toast.makeText(MainActivity.this, "Downloading...", Toast.LENGTH_SHORT).show();
                } catch (Exception e) { Toast.makeText(MainActivity.this, "Download failed", Toast.LENGTH_LONG).show(); }
            }
        });
        web.addJavascriptInterface(new Bridge(), "FeedlineApp");      // lets the Settings page switch phone notifications on and off
        handle(getIntent());
        startWatching();
    }

    @Override protected void onNewIntent(Intent i) { super.onNewIntent(i); handle(i); }

    private void handle(Intent i) {
        String url = prefs.getString("url", null);
        String printer = i.getStringExtra("printer");               // a notification was tapped: open that printer
        if (ACTION_CHANGE.equals(i.getAction()) || url == null) askAddress(url == null ? "" : url);
        else if (printer != null) web.loadUrl(Uri.parse(url).buildUpon().clearQuery().appendQueryParameter("p", printer).build().toString());
        else if (web.getUrl() == null) web.loadUrl(url);
    }

    /** Print notifications: needs the notification permission on Android 13+, then a small foreground service. */
    private boolean askedPermission;

    private void startWatching() {
        if (prefs.getString("url", null) == null || !prefs.getBoolean("notify", true)) return;
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            if (!askedPermission) { askedPermission = true; requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 9); }
            return;
        }
        try { NotifyService.start(this); } catch (RuntimeException e) { /* not allowed right now; tried again when the app is next opened */ }
    }

    @Override public void onRequestPermissionsResult(int req, String[] perms, int[] res) {
        super.onRequestPermissionsResult(req, perms, res);
        if (req == 9 && res.length > 0 && res[0] == PackageManager.PERMISSION_GRANTED) startWatching();
        else if (req == 9) Toast.makeText(this, "Notifications are off. You can allow them in Android settings.", Toast.LENGTH_LONG).show();
    }

    /** Offered once: without this Android may hold back notifications while the phone sleeps. */
    private void offerBatteryExemption() {
        if (Build.VERSION.SDK_INT < 23 || prefs.getBoolean("batt_asked", false)) return;
        PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
        if (pm.isIgnoringBatteryOptimizations(getPackageName())) return;
        prefs.edit().putBoolean("batt_asked", true).apply();
        new AlertDialog.Builder(this).setTitle("Deliver notifications on time?")
            .setMessage("Android can delay notifications while the phone sleeps. Allow Feedline to run in the background so print alerts arrive promptly.")
            .setPositiveButton("Allow", (d, w) -> {
                try { startActivity(new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:" + getPackageName()))); }
                catch (Exception e) { /* no such screen on this phone */ }
            }).setNegativeButton("Not now", null).show();
    }

    /** What the Settings page can call as window.FeedlineApp. */
    private class Bridge {
        @JavascriptInterface public boolean getNotify() { return prefs.getBoolean("notify", true); }
        @JavascriptInterface public void setNotify(final boolean on) {
            prefs.edit().putBoolean("notify", on).apply();
            runOnUiThread(() -> {
                if (on) { askedPermission = false; startWatching(); offerBatteryExemption(); }
                else stopService(new Intent(MainActivity.this, NotifyService.class));
            });
        }
    }

    private static String clean(String v) {
        v = v.trim();
        if (v.isEmpty()) return null;
        if (!v.matches("(?i)^https?://.*")) v = "http://" + v;
        if (!v.endsWith("/") && v.indexOf('/', 8) < 0) v += "/";
        return v;
    }

    private void askAddress(String current) {
        final EditText in = new EditText(this);
        in.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        in.setHint(EXAMPLE_URL);
        in.setText(current);
        in.setSelectAllOnFocus(true);
        new AlertDialog.Builder(this).setTitle("Feedline address")
            .setMessage("Where is your Feedline server? Enter its address, for example " + EXAMPLE_URL)
            .setView(in).setCancelable(prefs.getString("url", null) != null)
            .setPositiveButton("Open", (d, w) -> {
                String u = clean(in.getText().toString());
                if (u == null) { askAddress(current); return; }
                prefs.edit().putString("url", u).remove("last").apply();      // a new server starts its own event history
                web.loadUrl(u);
                startWatching();
            }).setNegativeButton("Cancel", null).show();
    }

    private AlertDialog errDialog;

    private void showError(String why) {
        if (errDialog != null) errDialog.dismiss();          // never stack error dialogs
        errDialog = new AlertDialog.Builder(this).setTitle("Feedline is not reachable")
            .setMessage(why + "\n\nCheck that you are on the home network (or VPN) and that the server is running.")
            .setPositiveButton("Retry", (d, w) -> web.loadUrl(prefs.getString("url", EXAMPLE_URL)))
            .setNeutralButton("Change address", (d, w) -> askAddress(prefs.getString("url", "")))
            .setNegativeButton("Close", null).show();
    }

    @Override protected void onActivityResult(int req, int res, Intent data) {
        if (req == REQ_FILE && chooser != null) {
            chooser.onReceiveValue(android.webkit.WebChromeClient.FileChooserParams.parseResult(res, data));
            chooser = null;
        } else super.onActivityResult(req, res, data);
    }

    @Override public void onBackPressed() {
        if (web.canGoBack()) web.goBack(); else super.onBackPressed();
    }

    @Override protected void onPause() { super.onPause(); web.onPause(); }
    @Override protected void onResume() { super.onResume(); web.onResume(); }
}
