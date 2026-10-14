package app.obsidianvow.core;

import java.util.*;
import java.util.function.BooleanSupplier;

/** One radio slot for both SK devices. Four channels refresh within 800 ms. */
final class SceneSkScheduler {
    static final long SLOT_MS=200;
    static final class Packet {
        final String driver,token,uuid;
        final long revision;
        final boolean stopping;
        Packet(Entry entry,boolean suction,int sequence) {
            driver=entry.driver;token=entry.token;revision=entry.revision;stopping=entry.stopping;
            boolean enabled=!stopping && (suction ? entry.values.get("suction_on")==1 : entry.values.get("pulse_gear")>0);
            int level=stopping?0:entry.values.get(suction?"suction_level":"pulse_gear");
            uuid=SceneSkCommands.command(driver,suction,enabled,level,sequence);
        }
    }
    private static final class Entry {
        final String driver,token;
        long revision;
        Map<String,Integer> values;
        BooleanSupplier valid;
        boolean stopping;
        final int[] stops={3,3};
        Entry(String driver,String token) { this.driver=driver;this.token=token; }
    }
    private final Map<String,Entry> entries=new LinkedHashMap<>();
    private long nextAt;
    private int cursor;
    void update(String driver,String token,long revision,Map<String,Integer> values,BooleanSupplier valid) {
        SceneProgram.require(driver.equals("sk30_ble") || driver.equals("sk40_ble"));
        if(!valid.getAsBoolean()) return;
        Entry entry=entries.get(driver);
        if(entry==null || !entry.token.equals(token) || entry.stopping) {
            entry=new Entry(driver,token);entries.put(driver,entry);
        } else if(revision<entry.revision) return;
        entry.revision=revision;entry.values=new LinkedHashMap<>(values);entry.valid=valid;
    }
    void stop(String token) {
        for(Entry entry:entries.values()) if(entry.token.equals(token)) entry.stopping=true;
    }
    boolean hasWork() { return !entries.isEmpty(); }
    Packet next(long now,int sequence) {
        if(entries.isEmpty() || now<nextAt) return null;
        List<Entry> ordered=new ArrayList<>(entries.values());
        for(int attempt=0;attempt<ordered.size()*2;attempt++) {
            int slot=cursor++ % (ordered.size()*2);
            Entry entry=ordered.get(slot/2);int channel=slot%2;
            if(!entry.stopping && !entry.valid.getAsBoolean()) entry.stopping=true;
            if(entry.stopping && entry.stops[channel]==0) continue;
            Packet packet=new Packet(entry,channel==0,sequence);
            if(entry.stopping && --entry.stops[channel]>=0 && entry.stops[0]+entry.stops[1]==0) entries.remove(entry.driver);
            nextAt=now+SLOT_MS;
            return packet;
        }
        return null;
    }
}
