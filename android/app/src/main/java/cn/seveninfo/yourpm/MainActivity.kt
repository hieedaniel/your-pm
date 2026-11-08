package cn.seveninfo.yourpm

import android.app.Activity
import android.app.AlertDialog
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.os.Bundle
import android.view.Menu
import android.view.MenuItem
import android.view.ViewGroup
import android.webkit.WebChromeClient
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.Toast

/**
 * 一个极薄的壳：WebView 打开服务端页面，原生层只提供麦克风。
 *
 * 为什么不做原生 UI：主题、字幕、卡片这些交互已经在网页里调好了，
 * 重写一遍意味着两套界面各自维护、各自出 bug。壳的价值在于「拿到麦克风 + 装成 app」。
 */
class MainActivity : Activity() {

    companion object {
        const val DEFAULT_URL = "https://se.seveninfo.cn/m"
        private const val PREF = "yourpm"
        private const val KEY_URL = "server_url"
        private const val REQ_MIC = 1001
    }

    private lateinit var web: WebView
    private lateinit var bridge: AudioBridge
    private var pendingMic: ((Boolean) -> Unit)? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val root = FrameLayout(this)
        web = WebView(this)
        root.addView(
            web,
            FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
        )
        setContentView(root)

        val s = web.settings
        s.javaScriptEnabled = true
        s.domStorageEnabled = true            // 页面用 localStorage 记住语种等偏好
        s.mediaPlaybackRequiresUserGesture = false
        s.cacheMode = WebSettings.LOAD_DEFAULT
        s.mixedContentMode = WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE
        s.loadWithOverviewMode = false
        s.useWideViewPort = false             // 页面自带 viewport，别让 WebView 再缩放
        s.userAgentString = s.userAgentString + " YourPMApp/1.0"

        web.webViewClient = WebViewClient()
        web.webChromeClient = WebChromeClient()

        bridge = AudioBridge(this, web)
        web.addJavascriptInterface(bridge, "QSNative")

        val saved = prefs().getString(KEY_URL, "") ?: ""
        if (saved.isBlank()) askUrl(DEFAULT_URL, firstRun = true) else load(saved)
    }

    private fun prefs(): SharedPreferences = getSharedPreferences(PREF, MODE_PRIVATE)

    private fun load(url: String) {
        web.loadUrl(url)
    }

    private fun askUrl(default: String, firstRun: Boolean) {
        val input = EditText(this).apply {
            setText(default)
            setSingleLine(true)
            setPadding(56, 28, 56, 28)
        }
        AlertDialog.Builder(this)
            .setTitle("服务端地址")
            .setMessage("填部署好的服务地址，例如 https://se.seveninfo.cn/")
            .setView(input)
            .setCancelable(!firstRun)
            .setPositiveButton("连接") { _, _ ->
                var u = input.text.toString().trim()
                if (u.isEmpty()) u = DEFAULT_URL
                if (!u.startsWith("http://") && !u.startsWith("https://")) u = "https://$u"
                if (!u.endsWith("/")) u += "/"
                prefs().edit().putString(KEY_URL, u).apply()
                load(u)
            }
            .setNegativeButton("取消") { _, _ -> if (firstRun) finish() }
            .show()
    }

    /** 给 AudioBridge 用：权限申请必须走 Activity，且结果回调到 onRequestPermissionsResult。 */
    fun requestMic(cb: (Boolean) -> Unit) {
        if (checkSelfPermission(android.Manifest.permission.RECORD_AUDIO)
            == PackageManager.PERMISSION_GRANTED
        ) {
            cb(true)
            return
        }
        pendingMic = cb
        requestPermissions(arrayOf(android.Manifest.permission.RECORD_AUDIO), REQ_MIC)
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQ_MIC) return
        val granted = grantResults.isNotEmpty() &&
                grantResults[0] == PackageManager.PERMISSION_GRANTED
        pendingMic?.invoke(granted)
        pendingMic = null
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menu.add(0, 1, 0, "重新加载")
        menu.add(0, 2, 1, "切换服务器")
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        when (item.itemId) {
            1 -> {
                web.reload()
                return true
            }
            2 -> {
                askUrl(prefs().getString(KEY_URL, DEFAULT_URL) ?: DEFAULT_URL, firstRun = false)
                return true
            }
        }
        return super.onOptionsItemSelected(item)
    }

    @Deprecated("WebView 自己处理返回栈，比直接退出更符合直觉")
    override fun onBackPressed() {
        if (web.canGoBack()) web.goBack() else super.onBackPressed()
    }

    override fun onDestroy() {
        try {
            bridge.stop()
        } catch (_: Exception) {
        }
        try {
            web.destroy()
        } catch (_: Exception) {
        }
        super.onDestroy()
    }
}
