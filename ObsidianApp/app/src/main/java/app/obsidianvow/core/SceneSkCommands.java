package app.obsidianvow.core;

import java.util.Locale;

/** Verified Cachito UUIDs; retain the original 0..100 sliders, not legacy 10-level mapping. */
final class SceneSkCommands {
    static String command(String driver, boolean suction, boolean enabled, int level, int sequence) {
        SceneProgram.require(driver.equals("sk30_ble") || driver.equals("sk40_ble"));
        SceneProgram.require(level>=0 && level<=100 && sequence>=0x64 && sequence<=0xff);
        boolean four=driver.equals("sk40_ble"), run=enabled && (suction || level>0);
        int duration=suction ? (four ? (run?0x51:0x01) : (run?0x82:0x02))
                             : (four ? (run?0x52:0x02) : (run?0x81:0x01));
        String payload="6400000002";
        if(run && suction) payload=String.format(Locale.ROOT,"640000%02x02",50+level/2);
        if(run && !suction) {
            // Integer arithmetic avoids floating-point off-by-one at exact steps.
            int intensity=four ? 102-level : (6000-57*level)/100;
            payload=String.format(Locale.ROOT,four?"2d%02x150002":"0a%02x1c0002",intensity);
        }
        String uuid=String.format(Locale.ROOT,"7100%02x%02x-%02x00-0000-0100-%s",four?0x17:0x0b,sequence,duration,payload);
        String hex=uuid.replace("-","");int sum=0;
        for(int i=0;i<hex.length();i+=2) sum+=Integer.parseInt(hex.substring(i,i+2),16);
        return uuid+String.format(Locale.ROOT,"%02x",sum&0xff);
    }
}
