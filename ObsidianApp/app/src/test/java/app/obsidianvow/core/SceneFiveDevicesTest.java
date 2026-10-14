package app.obsidianvow.core;

import org.junit.Test;
import java.util.*;
import static org.junit.Assert.*;
import static app.obsidianvow.core.SceneOutputPlayerTest.*;

public class SceneFiveDevicesTest {
    static Map<String,Integer> sk(int on,int suction,int pulse) {
        Map<String,Integer> result=new LinkedHashMap<>();
        result.put("suction_on",on);result.put("suction_level",suction);result.put("pulse_gear",pulse);return result;
    }
    static Map<String,Integer> cx(int vm,int vs,int sm,int ss) {
        Map<String,Integer> result=new LinkedHashMap<>();
        result.put("vibration_mode",vm);result.put("vibration_speed",vs);
        result.put("suction_mode",sm);result.put("suction_speed",ss);return result;
    }
    static Map<String,Object> program(String driver,long version,Map<String,Integer> values) {
        return device(driver,driver,version,Collections.singletonList(obj("duration_ms",1500,"values",values)));
    }
    @Test public void allFiveDevicesLoopAndOnlyOneDeviceReplacementResetsItsPhase() {
        long[] now={0};SceneOutputPlayer player=new SceneOutputPlayer(()->now[0]);
        Map<String,String> bindings=new LinkedHashMap<>();
        bindings.put("muse","muse_ble");bindings.put("sosexy","sosexy_ble");
        for(String driver:Arrays.asList("sk30_ble","sk40_ble","cx492b_ble")) bindings.put(driver,driver);
        String token=player.start("scene_test",0,"owner",bindings);
        for(int second=0;second<=600;second++) {
            now[0]=second*1000L;
            assertTrue(player.accept(player.request(),reply(0,muse(1,9,9),sosexy(1,100,100,100),
                    program("sk30_ble",1,sk(1,100,1)),program("sk40_ble",second<180?1:2,sk(1,0,100)),
                    program("cx492b_ble",1,cx(11,10,5,10)))));
            List<Map<String,Object>> frames=player.frames();assertEquals(5,frames.size());
            for(Map<String,Object> frame:frames) assertTrue(player.permits(token,(String)frame.get("driver"),(long)frame.get("revision")));
        }
        assertEquals(420000L,SceneProgram.map(player.adopted().get("sk40_ble")).get("elapsed_ms"));
        assertEquals(600000L,SceneProgram.map(player.adopted().get("sk30_ble")).get("elapsed_ms"));
        assertEquals(600000L,SceneProgram.map(player.adopted().get("cx492b_ble")).get("elapsed_ms"));
        player.stop();assertTrue(player.frames().isEmpty());
        assertFalse(player.permits(token,"sk40_ble",1));
        assertTrue(player.start("scene_test",0,"owner",bindings).isEmpty());
    }
    @Test public void cxAllModesAndStopsAreEncodedWithoutResettingTheOtherChannel() {
        for(int mode=1;mode<=11;mode++) for(int strength=1;strength<=10;strength++) {
            List<SceneChannelDelta.Command> commands=SceneChannelDelta.cx492b(Collections.emptyMap(),cx(mode,strength,5,10));
            assertEquals(String.format(Locale.ROOT,"55030000%02x%02x00",mode,strength),commands.get(0).hex);
            assertEquals("55090000050a00",commands.get(1).hex);
        }
        List<SceneChannelDelta.Command> delta=SceneChannelDelta.cx492b(cx(11,10,5,10),cx(11,0,5,10));
        assertEquals(1,delta.size());assertEquals("55030000000000",delta.get(0).hex);
        assertTrue(SceneChannelDelta.cx492b(cx(11,0,5,10),cx(11,0,5,10)).isEmpty());
        assertEquals("55090000000000",SceneChannelDelta.cx492b(cx(11,0,5,10),cx(11,0,5,0)).get(0).hex);
        for(int mode=1;mode<=5;mode++) {
            assertEquals(String.format(Locale.ROOT,"55090000%02x0100",mode),
                    SceneChannelDelta.cx492b(Collections.emptyMap(),cx(1,0,mode,1)).get(1).hex);
        }
    }
    @Test public void newDeviceChannelsAlternateOverlapAndStopAcrossLoopBoundaries() {
        Map<String,Object> sk= device("sk30","sk30_ble",1,Arrays.asList(
                obj("duration_ms",1500,"values",sk(1,0,100)),
                obj("duration_ms",1500,"values",sk(0,0,100)),
                obj("duration_ms",1500,"values",sk(1,100,0))));
        Map<String,Object> cx= device("cx492b","cx492b_ble",1,Arrays.asList(
                obj("duration_ms",1500,"values",cx(11,10,5,10)),
                obj("duration_ms",1500,"values",cx(11,0,5,10)),
                obj("duration_ms",1500,"values",cx(1,1,5,0))));
        SceneProgram skProgram=new SceneProgram(sk), cxProgram=new SceneProgram(cx);
        for(int step=0;step<30;step++) {
            Map<String,Integer> skFrame=skProgram.values(step*1500L),cxFrame=cxProgram.values(step*1500L);
            assertEquals(Integer.valueOf(step%3==1?0:1),skFrame.get("suction_on"));
            assertEquals(Integer.valueOf(step%3==2?0:100),skFrame.get("pulse_gear"));
            assertEquals(Integer.valueOf(step%3==0?10:step%3==1?0:1),cxFrame.get("vibration_speed"));
            assertEquals(Integer.valueOf(step%3==2?0:10),cxFrame.get("suction_speed"));
        }
    }
    @Test public void cachitoGoldenPacketsMatchCapturedProtocolWithTheExistingPhoneId() {
        assertEquals("71000ba5-8200-0000-0100-6400004b0255",SceneSkCommands.command("sk30_ble",true,true,50,0xa5));
        // The old hand-written nRF example had a checksum typo (79); sum of
        // its first 15 bytes is 92 with ade2, or 03 with the app's 0000 ID.
        assertEquals("71000ba5-8100-0000-0100-0a381c000203",SceneSkCommands.command("sk30_ble",false,true,7,0xa5));
        assertEquals("710017a5-5100-0000-0100-6400004b0230",SceneSkCommands.command("sk40_ble",true,true,50,0xa5));
        assertEquals("71000ba5-0200-0000-0100-64000000028a",SceneSkCommands.command("sk30_ble",true,false,100,0xa5));
        assertEquals("71000ba5-0100-0000-0100-640000000289",SceneSkCommands.command("sk30_ble",false,false,100,0xa5));
        assertEquals("710017a5-0100-0000-0100-640000000295",SceneSkCommands.command("sk40_ble",true,false,100,0xa5));
        assertEquals("710017a5-0200-0000-0100-640000000296",SceneSkCommands.command("sk40_ble",false,false,100,0xa5));
    }
    @Test public void cachitoZeroSuctionStillRunsAndPulseDirectionIsNotInverted() {
        for(String driver:Arrays.asList("sk30_ble","sk40_ble")) {
            assertTrue(SceneSkCommands.command(driver,true,true,0,0x64).contains("6400003202"));
            assertTrue(SceneSkCommands.command(driver,true,true,100,0x64).contains("6400006402"));
            String fast=SceneSkCommands.command(driver,false,true,1,0x64),slow=SceneSkCommands.command(driver,false,true,100,0x64);
            assertTrue(fast.contains(driver.equals("sk30_ble")?"0a3b1c0002":"2d65150002"));
            assertTrue(slow.contains(driver.equals("sk30_ble")?"0a031c0002":"2d02150002"));
            assertEquals(SceneSkCommands.command(driver,false,false,0,0x64),SceneSkCommands.command(driver,false,true,0,0x64));
            for(int level=0;level<=100;level++) for(boolean suction:new boolean[]{false,true}) {
                String hex=SceneSkCommands.command(driver,suction,true,level,0xff).replace("-","");
                assertEquals(32,hex.length());int sum=0;
                for(int i=0;i<30;i+=2) sum+=Integer.parseInt(hex.substring(i,i+2),16);
                assertEquals(sum&255,Integer.parseInt(hex.substring(30),16));
            }
        }
    }
    @Test public void fourSkChannelsRefreshWithin800msAndChangingOneDoesNotResetAnother() {
        SceneSkScheduler scheduler=new SceneSkScheduler();
        scheduler.update("sk30_ble","token",1,sk(1,0,100),()->true);
        scheduler.update("sk40_ble","token",1,sk(1,100,1),()->true);
        Map<String,Long> last=new HashMap<>();
        for(long now=0;now<10000;now+=200) {
            if(now==2000) scheduler.update("sk30_ble","token",2,sk(0,0,100),()->true);
            SceneSkScheduler.Packet packet=scheduler.next(now,0x64+(int)(now/200)%156);
            assertNotNull(packet);assertNull(scheduler.next(now+199,0xa5));
            String channel=packet.driver+packet.uuid.substring(9,13);
            if(now<2000 && last.containsKey(channel)) assertTrue(now-last.get(channel)<=800);
            last.put(channel,now);
            if(packet.driver.equals("sk40_ble")) { assertEquals(1,packet.revision);assertFalse(packet.stopping); }
        }
        scheduler.stop("token");int stops=0;
        while(scheduler.hasWork()) {
            SceneSkScheduler.Packet packet=scheduler.next(10000+stops*200,0xa5);
            assertNotNull(packet);assertTrue(packet.stopping);stops++;
            assertTrue(stops<=12);
        }
        assertEquals(12,stops);
    }
    @Test public void expiredFramesBecomeStopsAndLateUpdatesCannotRestartThem() {
        boolean[] valid={true};SceneSkScheduler scheduler=new SceneSkScheduler();
        scheduler.update("sk30_ble","old",1,sk(1,100,1),()->valid[0]);
        assertFalse(scheduler.next(0,0xa5).stopping);valid[0]=false;
        scheduler.update("sk30_ble","old",2,sk(1,100,1),()->false);
        assertTrue(scheduler.next(200,0xa5).stopping);
        scheduler.update("sk30_ble","new",1,sk(0,0,100),()->true);
        scheduler.stop("old");
        SceneSkScheduler.Packet next=scheduler.next(400,0xa5);
        assertEquals("new",next.token);assertFalse(next.stopping);
    }
    @Test public void newDeviceLimitsRejectOutOfRangeAndFloatsRatherThanClamping() {
        for(String driver:Arrays.asList("sk30_ble","sk40_ble","cx492b_ble")) {
            Map<String,Integer> values=new LinkedHashMap<>(SceneProgram.limits(driver));
            new SceneProgram(program(driver,1,values));
            for(String channel:values.keySet()) {
                Map<String,Integer> invalid=new LinkedHashMap<>(values);invalid.put(channel,values.get(channel)+1);
                try { new SceneProgram(program(driver,1,invalid));fail(); } catch(IllegalArgumentException expected) {}
            }
        }
    }
}
