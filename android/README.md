# 手机端 APK（WebView 壳 + 原生录音）

## 它是什么

一个几百 KB 的壳：用 WebView 打开服务端页面，唯一自己实现的是**麦克风采集**
（`AudioRecord`，16kHz / 单声道 / PCM16 / 200ms 一帧）。

页面发现 `window.QSNative` 存在时，跳过 `getUserMedia` 与 AudioWorklet，
改由原生层喂 PCM —— WS 上行、SSE 订阅、主题与卡片的渲染**完全复用网页那份代码**。
所以改界面只需要改 `index.html`，两端同时生效。

## 怎么构建

### 方式一：GitHub Actions（推荐，不用装任何 Android 工具）

push 到 `main` 且改动 `android/` 下文件即自动构建：

- 仓库 → **Actions** → `apk` → 最近一次 Run → 底部 **Artifacts → app-debug** 下载
- 打 `v*` 标签时，APK 还会自动挂到 Release 页

### 方式二：本地构建

需要 JDK 17 + Android SDK（`ANDROID_HOME` 指向 SDK）+ Gradle 8.7：

```bash
cd android
gradle assembleDebug          # 产物：app/build/outputs/apk/debug/app-debug.apk
```

工程里**故意不放 gradle wrapper**（`gradle-wrapper.jar` 是二进制，进仓库没意义），
所以本地要先有 `gradle`；没有的话用 SDKMAN 装一个，或直接用上面的 CI。

## 怎么装、怎么用

1. 手机允许「安装未知来源应用」，装上 `app-debug.apk`
2. 首次打开填服务端地址（默认 `https://se.seveninfo.cn/m`，手机精简版），之后可在右上角菜单改
3. 点「开始录音」→ 允许麦克风 → 说话，字幕与卡片实时出现

> 服务端必须是 **HTTPS** 或手机能直连的内网地址。用 `http://IP:19535` 时页面能开、
> 但因为录音走原生层，不受浏览器「安全上下文」限制，所以这种做法反而能用
> （浏览器版不行）。正式演示仍然建议 HTTPS。

## 关键文件

```
app/src/main/java/cn/seveninfo/yourpm/MainActivity.kt   WebView 壳、服务器地址、权限回调
app/src/main/java/cn/seveninfo/yourpm/AudioBridge.kt    AudioRecord 采集 + base64 推给页面
app/src/main/AndroidManifest.xml                        只申请 INTERNET 与 RECORD_AUDIO
```

## 排查

| 现象 | 原因 |
|---|---|
| 点录音没反应，日志区出现「麦克风权限被拒绝」 | 系统设置里手动允许录音权限 |
| 日志区没有「原生录音已开启」 | APK 没注入桥（可能是用浏览器打开了页面） |
| 有「原生录音已开启」但没字幕 | 服务端 ASR 密钥或网络问题，看服务端日志 |
| 电平条不动 | 采集线程没起来，多半是机型不支持 16kHz（极少见），看日志里的具体报错 |
