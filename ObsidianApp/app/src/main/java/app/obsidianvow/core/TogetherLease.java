package app.obsidianvow.core;

/** Monotonic local lease and server-time anchor; survives wall-clock changes. */
final class TogetherLease {
    private long receivedAt = -1;
    private long leaseMs = 45000;
    private double serverAt;
    private long sourceVersion = -1;
    private boolean active;

    void acknowledge(long monotonicNow, double serverNow, long durationMs, long version, boolean enabled) {
        receivedAt = monotonicNow; serverAt = serverNow; leaseMs = durationMs;
        sourceVersion = version; active = enabled;
    }
    boolean online(long now) { return active && receivedAt >= 0 && now >= receivedAt && now-receivedAt < leaseMs; }
    boolean accepts(long now, long version) { return online(now) && version == sourceVersion; }
    double serverNow(long now) { return receivedAt < 0 ? 0 : serverAt+Math.max(0, now-receivedAt)/1000.0; }
    void end() { active = false; }
}
