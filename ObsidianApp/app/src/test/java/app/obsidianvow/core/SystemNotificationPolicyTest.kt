package app.obsidianvow.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SystemNotificationPolicyTest {
    @Test fun unlockedPhoneUsesBriefBannerWhileLockedPhoneCanWake() {
        assertFalse(SystemNotificationPolicy.shouldUseFullScreen(true, false))
        assertTrue(SystemNotificationPolicy.shouldUseFullScreen(true, true))
        assertTrue(SystemNotificationPolicy.shouldUseFullScreen(false, true))
        assertTrue(SystemNotificationPolicy.shouldUseFullScreen(false, false))
    }

    @Test fun unlockingDoesNotRestartTheFiveMinuteNotificationLifetime() {
        val postedAt = 40_000L
        val expiresAt = postedAt + 300_000L
        assertEquals(288_000L, SystemNotificationPolicy.remainingLifetime(expiresAt, postedAt + 12_000L))
        assertEquals(60_000L, SystemNotificationPolicy.remainingLifetime(expiresAt, postedAt + 240_000L))
        assertEquals(0L, SystemNotificationPolicy.remainingLifetime(expiresAt, postedAt + 300_000L))
        assertEquals(0L, SystemNotificationPolicy.remainingLifetime(expiresAt, postedAt + 360_000L))
    }
}
