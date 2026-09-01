package app.obsidianvow.core


/** Pure permission selection kept Android-free so subset behavior is unit-testable. */
object HealthPermissionPolicy {
    @JvmStatic
    fun allowedKinds(
        permissionByKind: Map<String, String>,
        grantedPermissions: Set<String>,
    ): Set<String> {
        return permissionByKind.entries
            .filterTo(linkedSetOf()) { it.value in grantedPermissions }
            .mapTo(linkedSetOf()) { it.key }
    }
}
