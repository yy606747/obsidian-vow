"""Wiring checks supplement the JVM player/transport tests; not physical-device claims."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JAVA = ROOT / "ObsidianApp/app/src/main/java/app/obsidianvow/core"


def test_scene_service_is_declared_and_owns_background_clock_network_and_stop():
    service = (JAVA / "SceneForegroundService.java").read_text()
    bridge = (JAVA / "SceneDeviceBridge.java").read_text()
    manifest = (ROOT / "ObsidianApp/app/src/main/AndroidManifest.xml").read_text()
    assert 'android:name=".SceneForegroundService"' in manifest
    assert 'FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE' in service
    assert 'bridge.pulse()' in service and 'PARTIAL_WAKE_LOCK' in service
    assert 'wakeLock.release()' in service and 'START_NOT_STICKY' in service
    assert '"generation",generation' in service and '.setData(Uri.parse(' in service
    assert 'new SceneOutputPlayer(SystemClock::elapsedRealtime)' in bridge
    assert 'player.adopted()' in bridge and 'player.accept(nonce,value)' in bridge
    assert 'target.base+"/api/scenes/sessions/"' in bridge
    assert 'if(run!=target) return;' in bridge
    assert 'foreground && lease' not in bridge


def test_webview_lifetime_does_not_own_real_playback_and_recreation_attaches_shared_transport():
    activity = (JAVA / "WebViewActivity.java").read_text()
    bridge = (JAVA / "SceneDeviceBridge.java").read_text()
    assert 'SceneDeviceBridge.obtain(this)' in activity
    assert 'sceneDeviceBridge.detach(webView)' in activity
    assert 'sceneDeviceBridge.close()' not in activity
    assert 'bleBridge.shutdown()' not in activity
    foreground = bridge.split('void foreground(boolean value)', 1)[1].split('\n', 1)[0]
    assert 'stopInternal' not in foreground


def test_public_hardware_catalog_is_not_the_old_vibration_only_bench_profile():
    import asyncio
    from routes.scenes import hardware
    devices = asyncio.run(hardware())
    assert all(bounds['max'] > 0 for device in devices for bounds in device['capabilities']['channels'].values())


def test_multichannel_compiler_is_told_to_keep_other_channels_rhythms_not_freeze_them():
    from app.scenes.hardware import devices
    from app.scenes.prompts import compiler_messages
    prompt = compiler_messages(intent='联排', capabilities=devices()[1]['capabilities'], previous=None, position={})[0]['content']
    assert '共用这条时间轴' in prompt
    assert '不能只冻结在上一帧' in prompt


def test_new_devices_have_separate_connections_and_explicit_native_failure_fences():
    bridge = (JAVA / "SceneDeviceBridge.java").read_text()
    ble = (JAVA / "BleBridge.java").read_text()
    adv = (JAVA / "SceneSkTransport.java").read_text()
    legacy = (JAVA / "DomForegroundService.java").read_text()
    assert 'cx=new BleBridge(null,this.context)' in bridge
    assert 'cx.stopScene()' in bridge and 'ble.stopScene()' in bridge
    assert 'transport.sendScene(driver,target.token,values,' in bridge
    assert 'sk.stop(stopped.token)' in bridge and 'sk.pulse(now)' in bridge
    assert 'if(g!=gatt) return;' in ble
    assert 'SceneChannelDelta.cx492b(sceneApplied,values)' in ble
    assert '.setTimeout(900)' in adv
    assert 'SceneDeviceBridge.permits(packet.token,packet.driver,packet.revision)' in adv
    assert 'SceneDeviceBridge.active()' in legacy.split('private void advOnce', 1)[1]
