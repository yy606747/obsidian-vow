package app.obsidianvow.core;

import org.junit.Test;
import static org.junit.Assert.*;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;

public class TogetherLocalTest {
    @Test public void leaseExpiresWithoutEndingStoredSessionAndRejectsOldSource() {
        TogetherLease lease=new TogetherLease();
        assertFalse(lease.online(0));
        lease.acknowledge(1000,5000,45000,1,true);
        assertTrue(lease.accepts(44999,1));
        assertFalse(lease.online(46000));
        assertEquals(5045,lease.serverNow(46000),.001);
        lease.acknowledge(100000,9000,45000,2,true);
        assertFalse(lease.accepts(100100,1));
        assertTrue(lease.accepts(100100,2));
        lease.end();assertFalse(lease.online(100101));
    }
    @Test public void featureDisableAndMonotonicRebootCannotRenewOldLease() {
        TogetherLease lease=new TogetherLease();
        lease.acknowledge(1000,5000,45000,1,false);
        assertFalse(lease.online(1001));
        lease.acknowledge(1000,5000,45000,1,true);
        assertFalse(lease.online(200));
    }
    @Test public void recordingProducesPlayablePcmWavWithExactLength() {
        byte[] pcm={0,1,2,3,-1,-2};byte[] wav=TogetherAudio.wav(pcm);
        assertEquals(pcm.length+44,wav.length);
        assertEquals("RIFF",new String(wav,0,4,java.nio.charset.StandardCharsets.US_ASCII));
        ByteBuffer header=ByteBuffer.wrap(wav).order(ByteOrder.LITTLE_ENDIAN);
        assertEquals(pcm.length+36,header.getInt(4));
        assertEquals(1,header.getShort(20));assertEquals(1,header.getShort(22));
        assertEquals(16000,header.getInt(24));assertEquals(32000,header.getInt(28));
        assertEquals(16,header.getShort(34));assertEquals(pcm.length,header.getInt(40));
        assertArrayEquals(pcm,java.util.Arrays.copyOfRange(wav,44,wav.length));
    }
}
