// 只声明插件版本，模块里 apply。AGP 8.5.2 需要 Gradle 8.7+ 与 JDK 17。
plugins {
    id("com.android.application") version "8.5.2" apply false
    id("org.jetbrains.kotlin.android") version "1.9.24" apply false
}
