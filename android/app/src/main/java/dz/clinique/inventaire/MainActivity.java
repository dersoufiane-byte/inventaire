package dz.clinique.inventaire;

import android.Manifest;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.DhcpInfo;
import android.net.Uri;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.Bundle;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;
import androidx.webkit.WebViewAssetLoader;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.SocketTimeoutException;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.Iterator;
import java.util.List;
import java.util.Set;

public class MainActivity extends AppCompatActivity {

    private static final int REQ_CAMERA = 42;
    private static final int REQ_FILES = 43;
    private static final String HOST = "appassets.androidplatform.net";
    private static final int DISCOVERY_PORT = 8766;

    private WebView web;
    private PermissionRequest pendingRequest;
    private ValueCallback<Uri[]> fileCallback;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        web = new WebView(this);
        setContentView(web);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setAllowFileAccess(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);

        final WebViewAssetLoader loader = new WebViewAssetLoader.Builder()
                .setDomain(HOST)
                .addPathHandler("/assets/", new WebViewAssetLoader.AssetsPathHandler(this))
                .build();

        web.setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                return loader.shouldInterceptRequest(request.getUrl());
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if (HOST.equals(uri.getHost())) return false;
                try { startActivity(new Intent(Intent.ACTION_VIEW, uri)); } catch (Exception ignored) { }
                return true;
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(final PermissionRequest request) {
                runOnUiThread(() -> {
                    boolean wantsCamera = false;
                    for (String r : request.getResources()) {
                        if (PermissionRequest.RESOURCE_VIDEO_CAPTURE.equals(r)) wantsCamera = true;
                    }
                    if (!wantsCamera) { request.deny(); return; }
                    if (ContextCompat.checkSelfPermission(MainActivity.this, Manifest.permission.CAMERA)
                            == PackageManager.PERMISSION_GRANTED) {
                        request.grant(new String[]{PermissionRequest.RESOURCE_VIDEO_CAPTURE});
                    } else {
                        pendingRequest = request;
                        ActivityCompat.requestPermissions(MainActivity.this,
                                new String[]{Manifest.permission.CAMERA}, REQ_CAMERA);
                    }
                });
            }

            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (fileCallback != null) fileCallback.onReceiveValue(null);
                fileCallback = callback;
                Intent i = new Intent(Intent.ACTION_GET_CONTENT);
                i.addCategory(Intent.CATEGORY_OPENABLE);
                i.setType("image/*");
                i.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                try {
                    startActivityForResult(Intent.createChooser(i, "Choisir des photos"), REQ_FILES);
                } catch (Exception e) {
                    fileCallback = null;
                    return false;
                }
                return true;
            }
        });

        web.addJavascriptInterface(new Bridge(), "AndroidBridge");
        web.loadUrl("https://" + HOST + "/assets/index.html");
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQ_FILES || fileCallback == null) return;
        Uri[] result = null;
        if (resultCode == RESULT_OK && data != null) {
            if (data.getClipData() != null) {
                int n = data.getClipData().getItemCount();
                result = new Uri[n];
                for (int k = 0; k < n; k++) result[k] = data.getClipData().getItemAt(k).getUri();
            } else if (data.getData() != null) {
                result = new Uri[]{data.getData()};
            }
        }
        fileCallback.onReceiveValue(result);
        fileCallback = null;
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, @NonNull String[] permissions, @NonNull int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQ_CAMERA && pendingRequest != null) {
            if (grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                pendingRequest.grant(new String[]{PermissionRequest.RESOURCE_VIDEO_CAPTURE});
            } else {
                pendingRequest.deny();
                Toast.makeText(this, "Autorisez la caméra pour photographier les produits.", Toast.LENGTH_LONG).show();
            }
            pendingRequest = null;
        }
    }

    @Override
    public void onBackPressed() {
        if (web == null) { super.onBackPressed(); return; }
        web.evaluateJavascript("(window.__onBack && window.__onBack()) ? 'yes' : 'no'", value -> {
            if (value == null || !value.contains("yes")) finish();
        });
    }

    private void callback(final String id, final int status, final String text) {
        final String js = "window.__bridgeCb(" + JSONObject.quote(id) + "," + status + "," + JSONObject.quote(text == null ? "" : text) + ")";
        runOnUiThread(() -> { if (web != null) web.evaluateJavascript(js, null); });
    }

    /** Fonctions appelées par la page (index.html). */
    private class Bridge {

        @JavascriptInterface
        public String appVersion() {
            try {
                return getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
            } catch (Exception e) {
                return "2.0";
            }
        }

        @JavascriptInterface
        public String deviceName() {
            String m = Build.MANUFACTURER == null ? "" : Build.MANUFACTURER;
            String model = Build.MODEL == null ? "Téléphone" : Build.MODEL;
            if (!model.toLowerCase().startsWith(m.toLowerCase()) && !m.isEmpty())
                model = Character.toUpperCase(m.charAt(0)) + m.substring(1) + " " + model;
            return model;
        }

        /** Requête HTTP vers le PC (réseau local), réponse renvoyée par window.__bridgeCb. */
        @JavascriptInterface
        public void http(final String id, final String method, final String url, final String headersJson, final String body) {
            new Thread(() -> {
                HttpURLConnection c = null;
                try {
                    c = (HttpURLConnection) new URL(url).openConnection();
                    c.setRequestMethod(method);
                    c.setConnectTimeout(4000);
                    c.setReadTimeout(45000);
                    JSONObject h = new JSONObject(headersJson == null || headersJson.isEmpty() ? "{}" : headersJson);
                    Iterator<String> keys = h.keys();
                    while (keys.hasNext()) { String k = keys.next(); c.setRequestProperty(k, h.getString(k)); }
                    if (body != null && !body.isEmpty()) {
                        byte[] b = body.getBytes(StandardCharsets.UTF_8);
                        c.setDoOutput(true);
                        c.setFixedLengthStreamingMode(b.length);
                        try (OutputStream os = c.getOutputStream()) { os.write(b); }
                    }
                    int code = c.getResponseCode();
                    InputStream in = code >= 400 ? c.getErrorStream() : c.getInputStream();
                    String text = "";
                    if (in != null) {
                        ByteArrayOutputStream bo = new ByteArrayOutputStream();
                        byte[] buf = new byte[8192]; int n;
                        while ((n = in.read(buf)) > 0) bo.write(buf, 0, n);
                        in.close();
                        text = bo.toString("UTF-8");
                    }
                    callback(id, code, text);
                } catch (Exception e) {
                    callback(id, 0, "");
                } finally {
                    if (c != null) c.disconnect();
                }
            }).start();
        }

        /** Cherche le programme PC sur le Wi-Fi (diffusion UDP). */
        @JavascriptInterface
        public void discover(final String id) {
            new Thread(() -> {
                JSONArray found = new JSONArray();
                WifiManager.MulticastLock lock = null;
                DatagramSocket sock = null;
                try {
                    WifiManager wm = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
                    if (wm != null) { lock = wm.createMulticastLock("inventaire"); lock.acquire(); }
                    sock = new DatagramSocket();
                    sock.setBroadcast(true);
                    sock.setSoTimeout(400);
                    byte[] msg = "INVENTAIRE?".getBytes(StandardCharsets.UTF_8);
                    List<InetAddress> targets = new ArrayList<>();
                    targets.add(InetAddress.getByName("255.255.255.255"));
                    if (wm != null) {
                        DhcpInfo d = wm.getDhcpInfo();
                        if (d != null && d.ipAddress != 0) {
                            int bc = (d.ipAddress & d.netmask) | ~d.netmask;
                            byte[] q = new byte[4];
                            for (int k = 0; k < 4; k++) q[k] = (byte) ((bc >> (k * 8)) & 0xFF);
                            targets.add(InetAddress.getByAddress(q));
                        }
                    }
                    Set<String> seen = new HashSet<>();
                    long end = System.currentTimeMillis() + 2500;
                    int round = 0;
                    while (System.currentTimeMillis() < end) {
                        if (round++ % 3 == 0) {
                            for (InetAddress t : targets) {
                                try { sock.send(new DatagramPacket(msg, msg.length, t, DISCOVERY_PORT)); } catch (Exception ignored) { }
                            }
                        }
                        try {
                            byte[] buf = new byte[1024];
                            DatagramPacket p = new DatagramPacket(buf, buf.length);
                            sock.receive(p);
                            JSONObject o = new JSONObject(new String(p.getData(), 0, p.getLength(), StandardCharsets.UTF_8));
                            String ip = p.getAddress().getHostAddress();
                            if (!"inventaire-clinique".equals(o.optString("app")) || seen.contains(ip)) continue;
                            seen.add(ip);
                            JSONObject r = new JSONObject();
                            r.put("ip", ip);
                            r.put("port", o.optInt("port", 8765));
                            r.put("name", o.optString("name", "PC"));
                            found.put(r);
                        } catch (SocketTimeoutException ignored) { }
                    }
                } catch (Exception ignored) {
                } finally {
                    if (sock != null) sock.close();
                    if (lock != null && lock.isHeld()) lock.release();
                }
                callback(id, 200, found.toString());
            }).start();
        }
    }
}
