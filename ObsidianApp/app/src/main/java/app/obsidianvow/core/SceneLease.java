package app.obsidianvow.core;

import java.util.HashMap;
import java.util.Map;
import java.util.UUID;
import java.util.function.LongSupplier;

/** Pure local fence. A delayed network response cannot extend an expired lease. */
final class SceneLease {
    static final long TTL_MS = 3000;
    static final long MAX_RTT_MS = 1500;
    private final LongSupplier clock;
    private final Map<String, Long> closed = new HashMap<>();
    private final Map<String, Long> frames = new HashMap<>();
    private String session = "", token = "";
    private long generation, deadline, request, requestedAt;

    SceneLease(LongSupplier clock) { this.clock = clock; }

    synchronized String open(String id, long gen) {
        if (id == null || id.isEmpty() || gen < 0 || gen <= closed.getOrDefault(id, -1L)
                || (active() && session.equals(id) && gen <= generation)) return "";
        close();
        session = id; generation = gen; token = UUID.randomUUID().toString();
        deadline = clock.getAsLong() + TTL_MS;
        frames.clear();
        return token;
    }

    synchronized boolean alive(String value) {
        return !token.isEmpty() && token.equals(value) && clock.getAsLong() < deadline;
    }

    synchronized boolean active() { return !token.isEmpty(); }
    synchronized String token() { return token; }

    synchronized long request(String value) {
        if (!alive(value)) return 0;
        requestedAt = clock.getAsLong();
        return ++request;
    }

    synchronized boolean renew(String value, long nonce) {
        long now = clock.getAsLong();
        if (!alive(value) || nonce <= 0 || nonce != request || now - requestedAt > MAX_RTT_MS) return false;
        deadline = requestedAt + TTL_MS;
        requestedAt = -TTL_MS * 2; // A response can be consumed only once.
        return true;
    }

    synchronized boolean frame(String value, String driver, long revision) {
        if (!alive(value) || revision < frames.getOrDefault(driver, -1L)) return false;
        frames.put(driver, revision);
        return true;
    }

    synchronized boolean permits(String value, String driver, long revision) {
        return alive(value) && revision == frames.getOrDefault(driver, -1L);
    }

    synchronized void close() {
        if (!token.isEmpty()) closed.put(session, Math.max(generation, closed.getOrDefault(session, -1L)));
        token = ""; frames.clear();
    }
}
