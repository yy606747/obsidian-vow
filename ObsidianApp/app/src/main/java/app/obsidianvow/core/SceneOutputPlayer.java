package app.obsidianvow.core;

import java.util.*;
import java.util.function.LongSupplier;

/** Entire loop clock lives in native code. Background/UI/socket state is deliberately absent. */
final class SceneOutputPlayer {
    private final LongSupplier clock;
    final SceneLease lease;
    private final Map<String,Loop> loops = new LinkedHashMap<>();
    private String id = "", owner = "", token = "";
    private long generation;

    private static final class Loop {
        final String driver;
        SceneProgram program;
        Map<String,Integer> sent;
        long started, revision, lastSent = Long.MIN_VALUE / 2;
        Loop(String driver) { this.driver = driver; }
    }
    SceneOutputPlayer(LongSupplier clock) { this.clock=clock; lease=new SceneLease(clock); }
    synchronized String start(String id, long generation, String owner, Map<String,String> bindings) {
        SceneProgram.require(id.matches("scene_[A-Za-z0-9_]+") && !owner.isEmpty() && generation >= 0);
        SceneProgram.require(!bindings.isEmpty() && bindings.size() <= 5 && new HashSet<>(bindings.values()).size()==bindings.size());
        for (String driver : bindings.values()) SceneProgram.limits(driver);
        if (alive() && this.id.equals(id) && this.generation==generation && this.owner.equals(owner)) {
            SceneProgram.require(bindings.equals(bindings())); return token;
        }
        String granted = lease.open(id,generation);
        if (granted.isEmpty()) return "";
        this.id=id; this.owner=owner; this.generation=generation; token=granted;
        loops.clear(); for (Map.Entry<String,String> entry : bindings.entrySet()) loops.put(entry.getKey(),new Loop(entry.getValue()));
        return token;
    }
    synchronized long request() { return lease.request(token); }
    synchronized boolean accept(long nonce, Map<String,Object> raw) {
        if (!alive() || !id.equals(raw.get("id")) || SceneProgram.integer(raw.get("generation"),0,Long.MAX_VALUE)!=generation) return false;
        if (!Arrays.asList("active","preparing").contains(raw.get("status"))) return false;
        Object devices=raw.get("devices"); SceneProgram.require(devices instanceof List);
        List<SceneProgram> validated=new ArrayList<>(); Set<String> ids=new HashSet<>();
        for (Object item : (List<?>)devices) {
            SceneProgram program=new SceneProgram(SceneProgram.map(item));
            Loop loop=loops.get(program.id);
            SceneProgram.require(ids.add(program.id) && loop!=null && loop.driver.equals(program.driver));
            SceneProgram.require(loop.program==null || loop.program.capabilities.equals(program.capabilities));
            validated.add(program);
        }
        SceneProgram.require(ids.equals(loops.keySet()));
        if (!lease.renew(token,nonce)) return false;
        long now=clock.getAsLong();
        for (SceneProgram program : validated) {
            Loop loop=loops.get(program.id);
            if (loop.program!=null && program.version<=loop.program.version) continue;
            loop.program=program; loop.started=now;
            // Do not reset an unchanged hardware channel just because a whole
            // device sequence was replaced. Its software phase still restarts.
        }
        return true;
    }
    synchronized List<Map<String,Object>> frames() {
        List<Map<String,Object>> result=new ArrayList<>();
        if (!alive()) return result;
        long now=clock.getAsLong();
        for (Loop loop : loops.values()) {
            if (loop.program==null) continue;
            Map<String,Integer> values=loop.program.values(now-loop.started);
            boolean changed=!values.equals(loop.sent);
            if (!changed && now-loop.lastSent<500) continue;
            if (changed) loop.revision++;
            loop.sent=new LinkedHashMap<>(values); loop.lastSent=now;
            if (!lease.frame(token,loop.driver,loop.revision)) return Collections.emptyList();
            Map<String,Object> frame=new LinkedHashMap<>();
            frame.put("token",token); frame.put("driver",loop.driver); frame.put("revision",loop.revision); frame.put("values",loop.sent);
            result.add(frame);
        }
        return result;
    }
    synchronized Map<String,Object> adopted() {
        Map<String,Object> result=new LinkedHashMap<>();
        for (Map.Entry<String,Loop> entry : loops.entrySet()) {
            Loop loop=entry.getValue(); Map<String,Object> value=new LinkedHashMap<>();
            value.put("version",loop.program==null ? -1L : loop.program.version);
            value.put("elapsed_ms",loop.program==null || loop.program.steps.isEmpty() ? 0L : Math.max(0,clock.getAsLong()-loop.started));
            result.put(entry.getKey(),value);
        }
        return result;
    }
    synchronized Map<String,String> bindings() {
        Map<String,String> result=new LinkedHashMap<>();
        for (Map.Entry<String,Loop> entry:loops.entrySet()) result.put(entry.getKey(),entry.getValue().driver);
        return result;
    }
    synchronized boolean alive() { return lease.alive(token); }
    synchronized boolean permits(String token,String driver,long revision) { return lease.permits(token,driver,revision); }
    synchronized void stop() { lease.close(); }
}
