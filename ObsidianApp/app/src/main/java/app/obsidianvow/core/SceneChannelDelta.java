package app.obsidianvow.core;

import java.util.*;

/** Protocol deltas: changing one motor must not re-trigger another motor's preset. */
final class SceneChannelDelta {
    static final class Command {
        final String hex;
        final Map<String,Integer> values;
        Command(String hex,Map<String,Integer> values) { this.hex=hex;this.values=values; }
    }
    static List<Command> sosexy(Map<String,Integer> previous, Map<String,Integer> next) {
        List<Command> result=new ArrayList<>();
        String[] motors={"vibration","electro","suction"}, modes={"0002","0004","0008"}, speeds={"0001","0003","0007"};
        for(int i=0;i<motors.length;i++) {
            String mode=motors[i]+"_mode", speed=motors[i]+"_speed";
            Map<String,Integer> changed=new LinkedHashMap<>(); StringBuilder fields=new StringBuilder();
            if(!Objects.equals(previous.get(mode),next.get(mode))) { changed.put(mode,next.get(mode));fields.append(modes[i]).append("11").append(hex(next.get(mode))); }
            if(!Objects.equals(previous.get(speed),next.get(speed))) { changed.put(speed,next.get(speed));fields.append(speeds[i]).append("11").append(hex(next.get(speed))); }
            if(!changed.isEmpty()) result.add(new Command(hex(changed.size())+fields,changed));
        }
        return result;
    }
    static int[] muse(int previous,int next,int base) {
        if(previous==next) return new int[0];
        if(next==0) return new int[]{base};
        if(previous==0) return new int[]{base+next};
        return new int[]{base,base+next}; // Unknown or changing preset: stop this channel only.
    }
    static List<Command> cx492b(Map<String,Integer> previous, Map<String,Integer> next) {
        List<Command> result=new ArrayList<>();
        String[] motors={"vibration","suction"}, codes={"03","09"};
        for(int i=0;i<motors.length;i++) {
            String mode=motors[i]+"_mode", speed=motors[i]+"_speed";
            if(Objects.equals(previous.get(mode),next.get(mode)) && Objects.equals(previous.get(speed),next.get(speed))) continue;
            int output=next.get(speed), pattern=output==0 ? 0 : next.get(mode);
            Map<String,Integer> applied=new LinkedHashMap<>();
            applied.put(mode,next.get(mode));applied.put(speed,output);
            result.add(new Command("55"+codes[i]+"0000"+hex(pattern)+hex(output)+"00",applied));
        }
        return result;
    }
    private static String hex(int value) { return String.format(Locale.ROOT,"%02x",value); }
}
