package app.obsidianvow.core;

import org.junit.Test;
import java.util.*;
import static org.junit.Assert.*;

public class SceneChannelDeltaTest {
    @Test public void changingSuctionDoesNotResendVibrationOrElectroOrEvenItsUnchangedMode() {
        Map<String,Integer> old=SceneOutputPlayerTest.values(5,4,6), next=SceneOutputPlayerTest.values(5,4,8);
        List<SceneChannelDelta.Command> commands=SceneChannelDelta.sosexy(old,next);
        assertEquals(1,commands.size());assertEquals("0100071108",commands.get(0).hex);
        assertEquals(Collections.singletonMap("suction_speed",8),commands.get(0).values);
    }
    @Test public void stoppingOneChannelPreservesOtherMotorsAndPresets() {
        Map<String,Integer> old=SceneOutputPlayerTest.values(5,4,6), next=SceneOutputPlayerTest.values(0,4,6);
        assertEquals("0100011100",SceneChannelDelta.sosexy(old,next).get(0).hex);
        assertTrue(SceneChannelDelta.sosexy(old,new LinkedHashMap<>(old)).isEmpty());
    }
    @Test public void unknownAndInterruptedWritesAreReconciledOnlyForTheirOwnFields() {
        Map<String,Integer> next=SceneOutputPlayerTest.values(5,4,6), partial=new LinkedHashMap<>(next);
        assertEquals(3,SceneChannelDelta.sosexy(Collections.emptyMap(),next).size());
        partial.remove("electro_mode");
        List<SceneChannelDelta.Command> commands=SceneChannelDelta.sosexy(partial,next);
        assertEquals(1,commands.size());assertEquals("0100041101",commands.get(0).hex);
    }
    @Test public void museChangesOnlyTheSpecifiedChannel() {
        assertArrayEquals(new int[0],SceneChannelDelta.muse(2,2,0x40));
        assertArrayEquals(new int[]{0x30},SceneChannelDelta.muse(1,0,0x30));
        assertArrayEquals(new int[]{0x30,0x33},SceneChannelDelta.muse(1,3,0x30));
        assertArrayEquals(new int[]{0x40,0x42},SceneChannelDelta.muse(-1,2,0x40));
    }
}
