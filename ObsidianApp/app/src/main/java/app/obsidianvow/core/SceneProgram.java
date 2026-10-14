package app.obsidianvow.core;

import java.util.*;

/** Device-specific validation shared by every native playback update; no Android/UI state. */
final class SceneProgram {
    static final class Step {
        final int duration;
        final Map<String,Integer> values;
        Step(int duration, Map<String,Integer> values) {
            this.duration = duration;
            this.values = Collections.unmodifiableMap(new LinkedHashMap<>(values));
        }
    }
    final String id, driver;
    final long version;
    final Map<String,Object> capabilities;
    final Map<String,Integer> stopped;
    final List<Step> steps;
    final long duration;

    SceneProgram(Map<String,Object> raw) {
        id = string(raw.get("id")); driver = string(raw.get("driver"));
        version = integer(raw.get("version"), 0, Long.MAX_VALUE);
        capabilities = map(raw.get("capabilities"));
        Map<String,Object> channels = map(capabilities.get("channels"));
        Map<String,Integer> limits = limits(driver);
        require(channels.keySet().equals(limits.keySet()));
        stopped = new LinkedHashMap<>();
        Map<String,Integer> minimums = new LinkedHashMap<>(), maximums = new LinkedHashMap<>();
        for (String channel : limits.keySet()) {
            Map<String,Object> bounds = map(channels.get(channel));
            boolean mode = channel.endsWith("_mode");
            int min = (int)integer(bounds.get("min"), mode ? 1 : 0, limits.get(channel));
            int max = (int)integer(bounds.get("max"), min, limits.get(channel));
            require(Boolean.TRUE.equals(bounds.get("integer")) && (mode || min == 0));
            minimums.put(channel, min); maximums.put(channel, max); stopped.put(channel, mode ? min : 0);
        }
        int minStep = (int)integer(capabilities.get("min_step_ms"), driver.equals("muse_ble") ? 5000 : 1500, 60000);
        int maxStep = (int)integer(capabilities.get("max_step_ms"), minStep, 60000);
        long maxCycle = integer(capabilities.get("max_cycle_ms"), minStep, 600000);
        Map<String,Object> sequence = map(raw.get("sequence"));
        String action = string(sequence.get("action"));
        steps = new ArrayList<>();
        long total = 0;
        if (action.equals("replace")) {
            Object rawSteps = sequence.get("steps");
            require(rawSteps instanceof List);
            List<?> items = (List<?>)rawSteps;
            require(!items.isEmpty() && items.size() <= 128);
            for (Object item : items) {
                Map<String,Object> step = map(item), values = map(step.get("values"));
                require(values.keySet().equals(channels.keySet()));
                int ms = (int)integer(step.get("duration_ms"), minStep, maxStep);
                Map<String,Integer> clean = new LinkedHashMap<>();
                for (String key : channels.keySet()) clean.put(key, (int)integer(values.get(key), minimums.get(key), maximums.get(key)));
                steps.add(new Step(ms, clean)); total += ms;
            }
            require(total <= maxCycle);
        } else require(action.equals("stop"));
        duration = total;
    }
    Map<String,Integer> values(long elapsed) {
        if (steps.isEmpty()) return stopped;
        long offset = Math.max(0, elapsed) % duration;
        for (Step step : steps) {
            if (offset < step.duration) return step.values;
            offset -= step.duration;
        }
        throw new IllegalStateException("scene_position_invalid");
    }
    static Map<String,Integer> limits(String driver) {
        Map<String,Integer> result = new LinkedHashMap<>();
        if (driver.equals("muse_ble")) { result.put("vib_pattern",9); result.put("thrust_pattern",9); }
        else if (driver.equals("sosexy_ble")) {
            for (String motor : Arrays.asList("vibration","electro","suction")) {
                result.put(motor+"_mode",4); result.put(motor+"_speed",100);
            }
        } else if (driver.equals("cx492b_ble")) {
            result.put("vibration_mode",11); result.put("vibration_speed",10);
            result.put("suction_mode",5); result.put("suction_speed",10);
        } else {
            require(driver.equals("sk30_ble") || driver.equals("sk40_ble"));
            result.put("suction_on",1); result.put("suction_level",100); result.put("pulse_gear",100);
        }
        return result;
    }
    static long integer(Object value, long min, long max) {
        require(value instanceof Integer || value instanceof Long || value instanceof Short || value instanceof Byte);
        long number = ((Number)value).longValue(); require(number >= min && number <= max); return number;
    }
    @SuppressWarnings("unchecked") static Map<String,Object> map(Object value) {
        require(value instanceof Map); return (Map<String,Object>)value;
    }
    static String string(Object value) { require(value instanceof String && !((String)value).isEmpty()); return (String)value; }
    static void require(boolean ok) { if (!ok) throw new IllegalArgumentException("scene_sequence_invalid"); }
}
