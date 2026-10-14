"""Offline Java compile / pure JVM checks when Gradle's local socket is blocked.

Uses the installed SDK and cached dependency jars. Existing Kotlin classes and
generated R are reused; this checks Java integration, not resources, packaging,
APK installation, Android lifecycles or physical media capture.
"""
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

root = Path(__file__).resolve().parents[1]
app = root / "ObsidianApp/app"
sdk = Path((root / "ObsidianApp/local.properties").read_text().split("sdk.dir=", 1)[1].splitlines()[0])
cache = Path.home() / ".gradle/caches"
deps = list(cache.glob("transforms-4/*/transformed/*/jars/classes.jar"))
deps += [p for p in cache.glob("modules-2/files-2.1/**/*.jar") if any(
    name in str(p) for name in ("/org.jetbrains.kotlin/kotlin-stdlib", "/org.jetbrains.kotlinx/", "/com.squareup.okhttp3/", "/com.squareup.okio/", "/androidx.", "/org.jetbrains/annotations/", "/junit/", "/org.hamcrest/"))]
deps += [app / "build/tmp/kotlin-classes/debug", app / "build/intermediates/compile_and_runtime_not_namespaced_r_class_jar/debug/processDebugResources/R.jar"]
for path in deps[-2:]:
    if not path.exists():
        raise SystemExit(f"Missing generated input: {path}; run the normal Gradle build when available.")
ET.parse(app / "src/main/AndroidManifest.xml")
with tempfile.TemporaryDirectory(prefix="together-javac-") as output:
    boot = f"{sdk}/platforms/android-35/android.jar:{sdk}/build-tools/35.0.0/core-lambda-stubs.jar"
    args = ["javac", "-source", "8", "-target", "8", "-bootclasspath", boot,
            "-classpath", ":".join(map(str, deps)), "-d", output]
    result = subprocess.run(args + list(map(str, (app / "src/main/java").rglob("*.java"))))
    if result.returncode:
        raise SystemExit(result.returncode)
    print("All Android Java sources compiled against SDK 35 (cached Kotlin / R inputs).", flush=True)
    tests = [app / "src/test/java/app/obsidianvow/core/TogetherLocalTest.java"]
    cp = ":".join([output, *map(str, deps)])
    result = subprocess.run(["javac", "-source", "8", "-target", "8", "-classpath", cp, "-d", output, *map(str, tests)])
    if result.returncode:
        raise SystemExit(result.returncode)
    result = subprocess.run(["java", "-cp", cp, "org.junit.runner.JUnitCore", "app.obsidianvow.core.TogetherLocalTest"])
    raise SystemExit(result.returncode)
