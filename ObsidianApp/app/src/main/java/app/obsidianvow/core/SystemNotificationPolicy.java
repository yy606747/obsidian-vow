package app.obsidianvow.core;

/** System alerts may wake a locked display, but must not pin a banner while in use. */
final class SystemNotificationPolicy {
    private SystemNotificationPolicy() {}

    static boolean shouldUseFullScreen(boolean screenInteractive, boolean deviceLocked) {
        return !screenInteractive || deviceLocked;
    }

    static long remainingLifetime(long expiresAtElapsedMs, long nowElapsedMs) {
        return Math.max(0L, expiresAtElapsedMs - nowElapsedMs);
    }
}
