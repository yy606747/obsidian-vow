package app.obsidianvow.core

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test


class MessageNotificationPolicyTest {
    @Test
    fun suppressesOnlyWhenChatIsForegroundOnAnInteractiveUnlockedDevice() {
        assertFalse(MessageNotificationPolicy.shouldNotify(true, true, false))
        assertTrue(MessageNotificationPolicy.isChatActuallyVisible(true, true, false))
    }

    @Test
    fun notifiesForEveryBackgroundOrLockedCombination() {
        for (chatForeground in listOf(false, true)) {
            for (screenInteractive in listOf(false, true)) {
                for (deviceLocked in listOf(false, true)) {
                    if (chatForeground && screenInteractive && !deviceLocked) continue
                    assertTrue(
                        "foreground=$chatForeground interactive=$screenInteractive locked=$deviceLocked",
                        MessageNotificationPolicy.shouldNotify(
                            chatForeground,
                            screenInteractive,
                            deviceLocked,
                        ),
                    )
                    assertFalse(
                        MessageNotificationPolicy.isChatActuallyVisible(
                            chatForeground,
                            screenInteractive,
                            deviceLocked,
                        ),
                    )
                }
            }
        }
    }
}
