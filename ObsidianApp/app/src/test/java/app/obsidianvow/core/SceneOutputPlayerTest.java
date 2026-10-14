package app.obsidianvow.core;

import org.junit.Test;
import java.util.*;
import static org.junit.Assert.*;

public class SceneOutputPlayerTest {
    static Map<String,Object> obj(Object... pairs) {
        Map<String,Object> out=new LinkedHashMap<>();for(int i=0;i<pairs.length;i+=2)out.put((String)pairs[i],pairs[i+1]);return out;
    }
    static Map<String,Integer> values(int vibration,int electro,int suction) {
        Map<String,Integer> out=new LinkedHashMap<>();int[] speeds={vibration,electro,suction};int i=0;
        for(String name:Arrays.asList("vibration","electro","suction")){out.put(name+"_mode",1);out.put(name+"_speed",speeds[i++]);}return out;
    }
    static Map<String,Object> muse(long version,int vibration,int thrust) {
        return device("muse","muse_ble",version,Collections.singletonList(obj("duration_ms",5000,"values",obj("vib_pattern",vibration,"thrust_pattern",thrust))));
    }
    static Map<String,Object> sosexy(long version,int vibration,int electro,int suction) {
        return device("sosexy","sosexy_ble",version,Collections.singletonList(obj("duration_ms",1500,"values",values(vibration,electro,suction))));
    }
    static Map<String,Object> device(String id,String driver,long version,List<?> steps) {
        Map<String,Object> channels=new LinkedHashMap<>();
        for(Map.Entry<String,Integer> item:SceneProgram.limits(driver).entrySet())channels.put(item.getKey(),obj("min",item.getKey().endsWith("_mode")?1:0,"max",item.getValue(),"integer",true));
        return obj("id",id,"driver",driver,"version",version,"capabilities",obj("channels",channels,"min_step_ms",driver.equals("muse_ble")?5000:1500,"max_step_ms",60000,"max_cycle_ms",600000),"sequence",obj("action","replace","steps",steps));
    }
    static Map<String,Object> reply(long generation,Object... devices) { return obj("id","scene_test","generation",generation,"status","active","devices",Arrays.asList(devices)); }
    static SceneOutputPlayer start(long[] now) {
        SceneOutputPlayer player=new SceneOutputPlayer(()->now[0]);
        Map<String,String> bindings=new LinkedHashMap<>();bindings.put("muse","muse_ble");bindings.put("sosexy","sosexy_ble");
        assertFalse(player.start("scene_test",0,"owner",bindings).isEmpty());return player;
    }
    static Map<String,Object> frame(SceneOutputPlayer player,String driver) {
        return player.frames().stream().filter(f->driver.equals(f.get("driver"))).findFirst().orElseThrow(AssertionError::new);
    }
    @Test public void tenMinutesWithoutAWebViewContinuesLoopsAndReceivesNewPrograms() {
        long[] now={0};SceneOutputPlayer player=start(now);
        for(int second=0;second<=600;second++) {
            now[0]=second*1000L;
            assertTrue(player.accept(player.request(),reply(0,muse(second<180?1:2,second<180?1:3,2),sosexy(1,5,4,6))));
            List<Map<String,Object>> frames=player.frames();assertEquals(2,frames.size());
            assertEquals(second<180?1:3,SceneProgram.map(frames.get(0).get("values")).get("vib_pattern"));
        }
        assertEquals(420000L,SceneProgram.map(player.adopted().get("muse")).get("elapsed_ms"));
        assertEquals(600000L,SceneProgram.map(player.adopted().get("sosexy")).get("elapsed_ms"));
    }
    @Test public void sameDeviceChannelsShareTimelineAndCanStopIndividually() {
        long[] now={0};SceneOutputPlayer player=start(now);
        Map<String,Object> program=device("muse","muse_ble",1,Arrays.asList(
                obj("duration_ms",5000,"values",obj("vib_pattern",1,"thrust_pattern",2)),
                obj("duration_ms",5000,"values",obj("vib_pattern",0,"thrust_pattern",2)),
                obj("duration_ms",5000,"values",obj("vib_pattern",3,"thrust_pattern",0))));
        for(int second=0;second<=16;second++) {
            now[0]=second*1000L;assertTrue(player.accept(player.request(),reply(0,program,sosexy(1,5,4,6))));
            Map<String,Object> values=SceneProgram.map(frame(player,"muse_ble").get("values"));
            assertEquals(second%15<5?1:second%15<10?0:3,values.get("vib_pattern"));
            assertEquals(second%15<10?2:0,values.get("thrust_pattern"));
        }
    }
    @Test public void replacementRestartsOnlyChangedDeviceAndDoesNotRetriggerUnchangedValues() {
        long[] now={0};SceneOutputPlayer player=start(now);
        assertTrue(player.accept(player.request(),reply(0,muse(1,1,2),sosexy(1,5,4,6))));
        List<Map<String,Object>> first=player.frames();long rev=(long)first.get(0).get("revision");
        now[0]=1000;assertTrue(player.accept(player.request(),reply(0,muse(2,1,2),sosexy(1,5,4,6))));
        assertEquals(0L,SceneProgram.map(player.adopted().get("muse")).get("elapsed_ms"));
        assertEquals(1000L,SceneProgram.map(player.adopted().get("sosexy")).get("elapsed_ms"));
        assertEquals(rev,frame(player,"muse_ble").get("revision"));
    }
    @Test public void pauseAndExpiredRepliesNeverResurrectPlayback() {
        long[] now={0};SceneOutputPlayer player=start(now);long nonce=player.request();player.stop();
        assertFalse(player.accept(nonce,reply(0,muse(1,1,2),sosexy(1,5,4,6))));assertTrue(player.frames().isEmpty());
        assertTrue(player.start("scene_test",0,"owner",player.bindings()).isEmpty());
        assertFalse(player.start("scene_test",2,"owner",player.bindings()).isEmpty());
        long fresh=player.request();assertFalse(player.accept(nonce,reply(0,muse(1,1,2),sosexy(1,5,4,6))));
        assertTrue(player.accept(fresh,reply(2,muse(1,1,2),sosexy(1,5,4,6))));
        now[0]=4000;assertTrue(player.frames().isEmpty());assertFalse(player.accept(fresh,reply(2,muse(2,2,3),sosexy(2,1,1,1))));
    }
    @Test public void malformedMultiChannelProgramIsRejectedBeforeAnyDeviceAdopts() {
        long[] now={0};SceneOutputPlayer player=start(now);
        assertTrue(player.accept(player.request(),reply(0,muse(1,1,2),sosexy(1,5,4,6))));
        Map<String,Object> bad=sosexy(2,5,4,6);
        Map<String,Object> sequence=SceneProgram.map(bad.get("sequence"));
        Map<String,Object> step=SceneProgram.map(((List<?>)sequence.get("steps")).get(0));
        SceneProgram.map(step.get("values")).remove("electro_mode");
        try{player.accept(player.request(),reply(0,muse(2,3,3),bad));fail();}catch(IllegalArgumentException expected){}
        assertEquals(1L,SceneProgram.map(player.adopted().get("muse")).get("version"));
    }
    @Test public void physicalLimitsAndTransportTimingAreEnforced() {
        for(Object invalid:Arrays.asList(10,1.5,true,"1")) {
            Map<String,Object> raw=muse(1,1,2);Map<String,Object> step=SceneProgram.map(((List<?>)SceneProgram.map(raw.get("sequence")).get("steps")).get(0));
            SceneProgram.map(step.get("values")).put("vib_pattern",invalid);
            try{new SceneProgram(raw);fail();}catch(IllegalArgumentException expected){}
        }
    }
    @Test public void fullReversedRangesReachTheNativePlayerWithoutBenchClamping() {
        long[] now={0};SceneOutputPlayer player=start(now);
        Map<String,Integer> upper=values(100,100,100);
        for(String motor:Arrays.asList("vibration","electro","suction"))upper.put(motor+"_mode",4);
        Map<String,Object> all=device("sosexy","sosexy_ble",1,Collections.singletonList(
                obj("duration_ms",1500,"values",upper)));
        assertTrue(player.accept(player.request(),reply(0,muse(1,9,9),all)));
        List<Map<String,Object>> frames=player.frames();assertEquals(2,frames.size());
        Map<String,Object> museValues=SceneProgram.map(frames.stream().filter(f->"muse_ble".equals(f.get("driver"))).findFirst().orElseThrow(AssertionError::new).get("values"));
        Map<String,Object> sosexyValues=SceneProgram.map(frames.stream().filter(f->"sosexy_ble".equals(f.get("driver"))).findFirst().orElseThrow(AssertionError::new).get("values"));
        assertEquals(9,museValues.get("vib_pattern"));assertEquals(9,museValues.get("thrust_pattern"));
        assertEquals(upper,sosexyValues);
        player.stop();assertTrue(player.frames().isEmpty());
    }
    @Test public void threeMotorsOverlapAndRestIndependentlyAcrossCycleBoundaries() {
        long[] now={0};SceneOutputPlayer player=start(now);
        Map<String,Object> program=device("sosexy","sosexy_ble",1,Arrays.asList(
                obj("duration_ms",1500,"values",values(5,4,0)),
                obj("duration_ms",1500,"values",values(5,0,6)),
                obj("duration_ms",1500,"values",values(0,3,6))));
        for(int i=0;i<=20;i++) {
            now[0]=i*500L;assertTrue(player.accept(player.request(),reply(0,muse(1,1,2),program)));
            Map<String,Object> current=SceneProgram.map(frame(player,"sosexy_ble").get("values"));
            int step=(i/3)%3;
            assertEquals(step==2?0:5,current.get("vibration_speed"));
            assertEquals(step==0?4:step==1?0:3,current.get("electro_speed"));
            assertEquals(step==0?0:6,current.get("suction_speed"));
        }
    }
}
