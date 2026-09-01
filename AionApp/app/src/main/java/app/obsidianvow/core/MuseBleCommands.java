package app.obsidianvow.core;

import android.annotation.SuppressLint;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothManager;
import android.bluetooth.le.AdvertiseCallback;
import android.bluetooth.le.AdvertiseData;
import android.bluetooth.le.AdvertiseSettings;
import android.bluetooth.le.BluetoothLeAdvertiser;
import android.content.Context;
import android.os.ParcelUuid;
import android.util.Log;

import java.util.UUID;

@SuppressLint("MissingPermission")
final class MuseBleCommands {
    static final int STOP_REPEATS = 4;

    private static final int COMPANY_ID = 0x00ff;
    private static final UUID SERVICE_UUID = UUID.fromString("0000ae8f-0000-1000-8000-00805f9b34fb");
    private static final int ADV_DWELL_MS = 900;
    private static final int CMD_GAP_MS = 120;

    private MuseBleCommands() {}

    static BluetoothLeAdvertiser advertiser(Context context) {
        BluetoothManager bm = (BluetoothManager) context.getSystemService(Context.BLUETOOTH_SERVICE);
        if (bm == null) return null;
        BluetoothAdapter adapter = bm.getAdapter();
        return adapter == null ? null : adapter.getBluetoothLeAdvertiser();
    }

    static AdvertiseCallback callback(String tag) {
        return new AdvertiseCallback() {
            @Override
            public void onStartFailure(int errorCode) {
                Log.w(tag, "advertise failed: " + errorCode);
            }
        };
    }

    static void sendStopBurst(Context context, String tag) {
        BluetoothLeAdvertiser advertiser = advertiser(context);
        if (advertiser == null) {
            Log.w(tag, "BLE advertiser unavailable for ttl stop");
            return;
        }
        AdvertiseCallback callback = callback(tag);
        for (int i = 0; i < STOP_REPEATS; i++) advertiseCommand(advertiser, callback, 0x00, tag);
        stopCurrentAdv(advertiser, callback);
    }

    static void advertiseCommand(BluetoothLeAdvertiser advertiser, AdvertiseCallback callback, int command, String tag) {
        if (advertiser == null) return;
        try {
            stopCurrentAdv(advertiser, callback);
            sleepSafe(CMD_GAP_MS);
            AdvertiseSettings settings = new AdvertiseSettings.Builder()
                    .setAdvertiseMode(AdvertiseSettings.ADVERTISE_MODE_BALANCED)
                    .setTxPowerLevel(AdvertiseSettings.ADVERTISE_TX_POWER_HIGH)
                    .setConnectable(true)
                    .build();
            AdvertiseData data = new AdvertiseData.Builder()
                    .addServiceUuid(new ParcelUuid(SERVICE_UUID))
                    .addManufacturerData(COMPANY_ID, MuseBleEncoder.encodeCommand(command))
                    .setIncludeDeviceName(false)
                    .setIncludeTxPowerLevel(false)
                    .build();
            advertiser.startAdvertising(settings, data, callback);
            sleepSafe(ADV_DWELL_MS);
            stopCurrentAdv(advertiser, callback);
        } catch (Exception e) {
            Log.e(tag, "advertise command failed", e);
        }
    }

    static void stopCurrentAdv(BluetoothLeAdvertiser advertiser, AdvertiseCallback callback) {
        if (advertiser == null) return;
        try {
            advertiser.stopAdvertising(callback);
        } catch (Exception ignored) {
        }
    }

    private static void sleepSafe(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
