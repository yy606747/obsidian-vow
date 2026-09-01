package app.obsidianvow.core

import org.junit.Assert.assertEquals
import org.junit.Test


class HealthPermissionPolicyTest {
    private val permissions = linkedMapOf(
        "heart_rate" to "read-heart",
        "spo2" to "read-spo2",
        "sleep" to "read-sleep",
        "steps" to "read-steps",
    )

    @Test
    fun everyPermissionSubsetOnlyEnablesItsOwnRecordTypes() {
        val cases = listOf(
            emptySet<String>() to emptySet(),
            setOf("read-heart") to setOf("heart_rate"),
            setOf("read-heart", "read-steps") to setOf("heart_rate", "steps"),
            permissions.values.toSet() to permissions.keys.toSet(),
        )

        for ((granted, expected) in cases) {
            assertEquals(
                expected,
                HealthPermissionPolicy.allowedKinds(permissions, granted),
            )
        }
    }
}
