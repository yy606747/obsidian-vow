package app.obsidianvow.core;

/** Pure decision rule for suppressing notifications only while chat is truly visible. */
final class MessageNotificationPolicy {

    private MessageNotificationPolicy() {}

    static boolean shouldNotify(
            boolean chatForeground,
            boolean screenInteractive,
            boolean deviceLocked) {
        return !isChatActuallyVisible(chatForeground, screenInteractive, deviceLocked);
    }

    static boolean isChatActuallyVisible(
            boolean chatForeground,
            boolean screenInteractive,
            boolean deviceLocked) {
        return chatForeground && screenInteractive && !deviceLocked;
    }
}
