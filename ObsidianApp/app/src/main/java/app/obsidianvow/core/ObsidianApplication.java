package app.obsidianvow.core;

import android.app.Application;

public final class ObsidianApplication extends Application {
    @Override
    public void onCreate() {
        super.onCreate();
        BrandCompatibility.migratePreferences(this);
    }
}
