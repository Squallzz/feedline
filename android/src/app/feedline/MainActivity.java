package app.feedline;

import android.app.Activity;
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
    static final String DEFAULT_URL = "http://10.0.2.83:8081/";
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
        handle(getIntent());
    }

    @Override protected void onNewIntent(Intent i) { super.onNewIntent(i); handle(i); }

    private void handle(Intent i) {
        String url = prefs.getString("url", null);
        if (ACTION_CHANGE.equals(i.getAction()) || url == null) askAddress(url == null ? DEFAULT_URL : url);
        else if (web.getUrl() == null) web.loadUrl(url);
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
        in.setText(current);
        in.setSelectAllOnFocus(true);
        new AlertDialog.Builder(this).setTitle("Feedline address")
            .setMessage("Where is your Feedline server? For example http://192.168.1.60:8081/")
            .setView(in).setCancelable(prefs.getString("url", null) != null)
            .setPositiveButton("Open", (d, w) -> {
                String u = clean(in.getText().toString());
                if (u == null) { askAddress(current); return; }
                prefs.edit().putString("url", u).apply();
                web.loadUrl(u);
            }).setNegativeButton("Cancel", null).show();
    }

    private AlertDialog errDialog;

    private void showError(String why) {
        if (errDialog != null) errDialog.dismiss();          // never stack error dialogs
        errDialog = new AlertDialog.Builder(this).setTitle("Feedline is not reachable")
            .setMessage(why + "\n\nCheck that you are on the home network (or VPN) and that the server is running.")
            .setPositiveButton("Retry", (d, w) -> web.loadUrl(prefs.getString("url", DEFAULT_URL)))
            .setNeutralButton("Change address", (d, w) -> askAddress(prefs.getString("url", DEFAULT_URL)))
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
