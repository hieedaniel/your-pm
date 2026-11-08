package cn.seveninfo.yourpm

import android.Manifest
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Base64
import android.webkit.JavascriptInterface
import android.webkit.WebView
import kotlin.math.sqrt

/**
 * 原生音频采集桥。挂在 WebView 上的名字是 QSNative。
 *
 * 分工：这里只负责「把麦克风变成 16k/单声道/PCM16 的 200ms 帧」，
 * 走哪条 WS、怎么发、断线怎么重连，全部还是页面里的那套逻辑。
 * 这样浏览器版和 APK 版共享同一条链路，不会出现「手机上一套 bug」。
 */
class AudioBridge(
    private val activity: MainActivity,
    private val webView: WebView,
) {
    companion object {
        const val SAMPLE_RATE = 16000
        private const val FRAME_MS = 200
        private const val FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS / 1000   // 3200 样本
        private const val CHANNEL = AudioFormat.CHANNEL_IN_MONO
        private const val ENCODING = AudioFormat.ENCODING_PCM_16BIT
    }

    @Volatile
    private var running = false
    private var worker: Thread? = null
    private var recorder: AudioRecord? = null
    private var levelTick = 0

    /** 页面靠这个判断「是 APK 环境还是浏览器环境」。 */
    @JavascriptInterface
    fun available(): Boolean = true

    /**
     * 开始采集。返回：
     *  ok      已开始
     *  pending 正在向用户要麦克风权限，授权后自动开始
     *  already 已经在采了
     *  fail    设备不支持 / 初始化失败（原因已回写页面日志）
     */
    @JavascriptInterface
    fun start(): String {
        if (worker != null) return "already"
        if (activity.checkSelfPermission(Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED
        ) {
            activity.runOnUiThread {
                activity.requestMic { granted ->
                    if (granted) {
                        if (!startCapture()) warn("麦克风初始化失败")
                    } else {
                        warn("麦克风权限被拒绝，请在系统设置里允许录音")
                    }
                }
            }
            return "pending"
        }
        return if (startCapture()) "ok" else "fail"
    }

    @JavascriptInterface
    fun stop() {
        running = false
        val t = worker
        worker = null
        try {
            t?.join(500)
        } catch (_: InterruptedException) {
        }
        val r = recorder
        recorder = null
        if (r != null) {
            try {
                r.stop()
            } catch (_: Exception) {
            }
            try {
                r.release()
            } catch (_: Exception) {
            }
        }
        levelTick = 0
        js("window.__qsLevel&&window.__qsLevel(0)")
    }

    private fun startCapture(): Boolean {
        if (worker != null) return true
        val minBuf = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL, ENCODING)
        if (minBuf <= 0) {
            warn("这台设备不支持 16kHz 单声道采集")
            return false
        }
        val bufSize = maxOf(minBuf, FRAME_SAMPLES * 2 * 4)

        // VOICE_RECOGNITION 会做降噪与自动增益，远场说话更清楚；
        // 个别机型不支持这个 source，退回 MIC。
        var picked: AudioRecord? = null
        for (source in intArrayOf(
            MediaRecorder.AudioSource.VOICE_RECOGNITION,
            MediaRecorder.AudioSource.MIC
        )) {
            var r: AudioRecord? = null
            try {
                r = AudioRecord(source, SAMPLE_RATE, CHANNEL, ENCODING, bufSize)
            } catch (_: Exception) {
            }
            if (r != null && r.state == AudioRecord.STATE_INITIALIZED) {
                picked = r
                break
            }
            try {
                r?.release()
            } catch (_: Exception) {
            }
        }
        val rec = picked ?: run {
            warn("麦克风初始化失败（可能被其它应用占用）")
            return false
        }
        try {
            rec.startRecording()
        } catch (e: Exception) {
            try {
                rec.release()
            } catch (_: Exception) {
            }
            warn("启动录音失败：${e.message}")
            return false
        }
        recorder = rec
        running = true
        worker = Thread { loop(rec) }.also {
            it.priority = Thread.MAX_PRIORITY
            it.start()
        }
        return true
    }

    private fun loop(rec: AudioRecord) {
        val shorts = ShortArray(FRAME_SAMPLES)
        val bytes = ByteArray(FRAME_SAMPLES * 2)
        while (running) {
            val n = try {
                rec.read(shorts, 0, shorts.size)
            } catch (_: Exception) {
                -1
            }
            if (n <= 0) {
                if (!running) break
                if (n == AudioRecord.ERROR_INVALID_OPERATION || n == AudioRecord.ERROR_BAD_VALUE) break
                continue
            }
            var sum = 0.0
            var i = 0
            while (i < n) {
                val s = shorts[i].toInt()          // 有符号 16 位，别去掉符号位
                bytes[i * 2] = (s and 0xFF).toByte()
                bytes[i * 2 + 1] = ((s shr 8) and 0xFF).toByte()
                sum += (s * s).toDouble()
                i++
            }
            // 服务端按 200ms 一帧切，前端 worklet 也是这个口径；这里保持一致
            val b64 = Base64.encodeToString(bytes, 0, n * 2, Base64.NO_WRAP)
            js("window.__qsPcm&&window.__qsPcm('$b64')")
            levelTick++
            if (levelTick % 5 == 0) {              // 电平条每 1 秒刷一次就够
                val rms = sqrt(sum / n) / 32768.0
                js("window.__qsLevel&&window.__qsLevel($rms)")
            }
        }
    }

    /** 回写页面日志：出问题时用户能在页面的「日志」区直接看到原因。 */
    private fun warn(msg: String) {
        val safe = msg.replace("\\", "\\\\").replace("'", "\\'").replace("\n", " ")
        js("window.log&&window.log('原生录音：$safe','w')")
    }

    private fun js(code: String) {
        webView.post { webView.evaluateJavascript(code, null) }
    }
}
