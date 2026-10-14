package app.obsidianvow.core;

/** Monotonic local state machine. No network, UI or Android clock dependencies. */
final class LookAtMeSession {
    String id = "", convId = "", aiName = "", expression = "", phase = "queued";
    String decision = "", outcome = "", reason = "";
    long seconds, expires, choiceDeadline, start, end, actualMs, acceptedAt;
    long anchorElapsed, anchorWall, boot, sequence;
    long togetherHold;
    boolean notified;

    void anchorResponse(long serverNow, long expiresAt, long sent, long received) {
        // serverNow was sampled somewhere inside this round trip. Use its upper
        // bound at receipt so transport/long-poll time can only shorten validity.
        anchorElapsed = received;
        anchorWall = serverNow + Math.max(0, received - sent);
        expires = received + expiresAt - anchorWall;
    }

    long wall(long elapsed) { return anchorWall + elapsed - anchorElapsed; }
    boolean waiting() { return phase.equals("queued") || phase.equals("offered"); }
    boolean resident() { return phase.equals("active"); }
    boolean accepted() { return phase.equals("accepted"); }
    boolean live() { return !phase.equals("finished"); }
    long deadline() { return choiceDeadline > 0 ? choiceDeadline : expires; }
    boolean expired(long now) { return waiting() && now >= deadline(); }
    boolean startExpired(long now) { return accepted() && now - acceptedAt >= 15000; }

    boolean followDaily(String dailyId) {
        if ((!accepted() && !resident()) || dailyId.isEmpty() || dailyId.equals(convId)) return false;
        convId = dailyId;
        return true; // Ask for another visual handshake, retaining both timers.
    }

    boolean offer(long now) {
        if (!waiting() || expired(now)) return false;
        if (phase.equals("queued")) { phase = "offered"; choiceDeadline = now + 120000; }
        return true;
    }

    boolean decide(String value, long now) {
        if (!phase.equals("offered") || now >= choiceDeadline) return false;
        if (!value.equals("accepted") && !value.equals("rejected")) return false;
        decision = value;
        phase = value.equals("accepted") ? "accepted" : "finished";
        acceptedAt = now;
        return true;
    }

    boolean ready(String requestId, String conversationId, String dailyId, boolean visible, long now) {
        if (!accepted() || startExpired(now) || !visible || !id.equals(requestId) || dailyId.isEmpty()
                || !conversationId.equals(dailyId)) return false;
        convId = dailyId; phase = "active"; start = now; end = now + seconds * 1000;
        actualMs = 0;
        return true;
    }

    void observe(long now) { if (resident()) actualMs = Math.max(actualMs, Math.min(seconds * 1000, Math.max(0, now - start))); }

    void finish(String why, long now) {
        if (!live()) return;
        boolean active = resident();
        if (active) observe(now);
        if (active) outcome = now >= end && why.equals("deadline") ? "completed" : "ended_early";
        if (waiting() && expired(now) && !decision.equals("accepted") && choiceDeadline > 0) decision = "timed_out";
        reason = why; phase = "finished";
    }

    void recover() {
        // Never add the unobserved process gap or resume a residence.
        if (resident()) { outcome = "ended_early"; phase = "finished"; reason = "process_restart"; }
        else if (accepted()) { phase = "finished"; reason = "start_failed"; }
    }
}
