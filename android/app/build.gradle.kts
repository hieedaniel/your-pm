plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "cn.seveninfo.yourpm"
    compileSdk = 34

    defaultConfig {
        applicationId = "cn.seveninfo.yourpm"
        minSdk = 26              // AudioRecord / WebView 都需要；26 起步省掉一堆兼容分支
        targetSdk = 34
        versionCode = 1
        versionName = "1.0"
    }

    buildTypes {
        release {
            // 演示分发用 debug 签名即可（assembleDebug）。
            // 要正式发版再配 keystore，别把签名文件提交进仓库。
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }

    // 没有任何第三方依赖：WebView、AudioRecord、Base64 全是框架自带。
    // 好处是构建不依赖 Maven 能否连通，APK 体积也只有几百 KB。
}
