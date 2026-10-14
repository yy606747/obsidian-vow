package app.obsidianvow.core;

import java.io.ByteArrayOutputStream;

final class TogetherAudio {
    static byte[] wav(byte[] pcm) {
        ByteArrayOutputStream out = new ByteArrayOutputStream(pcm.length+44);
        writeText(out,"RIFF"); little(out,pcm.length+36,4); writeText(out,"WAVEfmt ");
        little(out,16,4); little(out,1,2); little(out,1,2); little(out,16000,4);
        little(out,32000,4); little(out,2,2); little(out,16,2); writeText(out,"data");
        little(out,pcm.length,4); out.write(pcm,0,pcm.length); return out.toByteArray();
    }
    private static void writeText(ByteArrayOutputStream out,String text) {
        for(char value:text.toCharArray()) out.write(value);
    }
    private static void little(ByteArrayOutputStream out,int number,int count) {
        for(int i=0;i<count;i++) out.write((number >>> (8*i)) & 255);
    }
}
