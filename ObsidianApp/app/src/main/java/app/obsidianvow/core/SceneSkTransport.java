package app.obsidianvow.core;

import android.annotation.SuppressLint;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothManager;
import android.bluetooth.le.*;
import android.content.Context;
import android.os.ParcelUuid;
import java.util.*;

/** Called under the scene lifecycle lock; no blocking radio writes or own playback clock. */
@SuppressLint("MissingPermission")
final class SceneSkTransport {
    private final Context context;
    private final SceneSkScheduler scheduler=new SceneSkScheduler();
    private int sequence=0x64+new Random().nextInt(0x9c);
    private BluetoothLeAdvertiser advertiser;
    private AdvertiseCallback callback;
    private volatile SceneSkScheduler.Packet current;
    private long emittedAt;
    SceneSkTransport(Context context) { this.context=context; }
    private BluetoothLeAdvertiser radio() {
        BluetoothManager manager=context.getSystemService(BluetoothManager.class);
        BluetoothAdapter adapter=manager==null?null:manager.getAdapter();
        return adapter==null||!adapter.isEnabled()?null:adapter.getBluetoothLeAdvertiser();
    }
    boolean supported() { try { return radio()!=null; } catch(Exception ignored) { return false; } }
    void update(String driver,String token,long revision,Map<String,Integer> values) {
        scheduler.update(driver,token,revision,values,()->SceneDeviceBridge.permits(token,driver,revision));
    }
    void stop(String token) {
        scheduler.stop(token);
        if(current!=null && current.token.equals(token)) clear();
    }
    boolean hasWork() { return scheduler.hasWork() || current!=null; }
    void pulse(long now) {
        SceneSkScheduler.Packet packet=scheduler.next(now,sequence);
        if(packet==null) {
            if(current!=null && now-emittedAt>=SceneSkScheduler.SLOT_MS && !scheduler.hasWork()) clear();
            return;
        }
        sequence=sequence==0xff?0x64:sequence+1;
        clear();
        if(!packet.stopping && !SceneDeviceBridge.permits(packet.token,packet.driver,packet.revision)) return;
        current=packet;emittedAt=now;
        callback=new AdvertiseCallback() {
            @Override public void onStartFailure(int code) {
                if(current==packet && !packet.stopping)
                    SceneDeviceBridge.transportFailed(packet.token,packet.driver,packet.revision);
            }
        };
        try {
            advertiser=radio();
            if(advertiser==null) throw new IllegalStateException("scene_bluetooth_unavailable");
            // A stalled service cannot leave the last run packet advertising forever.
            AdvertiseSettings settings=new AdvertiseSettings.Builder().setAdvertiseMode(AdvertiseSettings.ADVERTISE_MODE_LOW_LATENCY)
                    .setTxPowerLevel(AdvertiseSettings.ADVERTISE_TX_POWER_HIGH).setConnectable(false).setTimeout(900).build();
            AdvertiseData data=new AdvertiseData.Builder().addServiceUuid(new ParcelUuid(UUID.fromString(packet.uuid)))
                    .setIncludeDeviceName(false).setIncludeTxPowerLevel(false).build();
            advertiser.startAdvertising(settings,data,callback);
        } catch(Exception error) {
            if(!packet.stopping) SceneDeviceBridge.transportFailed(packet.token,packet.driver,packet.revision);
        }
    }
    private void clear() {
        current=null;
        if(advertiser!=null && callback!=null) try { advertiser.stopAdvertising(callback); } catch(Exception ignored) {}
        callback=null;
    }
}
