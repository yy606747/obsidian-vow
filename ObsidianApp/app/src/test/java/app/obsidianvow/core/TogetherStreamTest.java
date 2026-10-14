package app.obsidianvow.core;

import org.junit.Test;
import static org.junit.Assert.*;

public class TogetherStreamTest {
    @Test public void slowOrStaleSnapshotMustNotRemoveTheVisibleReply(){
        TogetherStream stream=new TogetherStream();
        assertEquals("慢慢走。",stream.append("first","慢慢走。"));
        assertEquals("慢慢走。\n\n我陪你。",stream.append("first","\n\n我陪你。"));
        stream.finish(15);
        assertFalse(stream.snapshotIncludesFinish(14));
        assertTrue(stream.snapshotIncludesFinish(15));
        assertFalse(stream.snapshotIncludesFinish(16));
    }
    @Test public void previousTurnSnapshotMustNotClearANewerStreamingReply(){
        TogetherStream stream=new TogetherStream();
        stream.append("first","上一句。\n\n还有一句。");
        stream.finish(10);
        assertEquals("下一句",stream.append("second","下一句"));
        assertFalse(stream.snapshotIncludesFinish(10));
        assertEquals("下一句也留在这里。",stream.append("second","也留在这里。"));
        stream.finish(20);
        assertFalse(stream.snapshotIncludesFinish(19));
        assertTrue(stream.snapshotIncludesFinish(21));
    }
    @Test public void interruptedTurnCanBeReplacedByItsSnapshotAndRetried(){
        TogetherStream stream=new TogetherStream();
        stream.append("interrupted","只来得及写这些");
        stream.finish(30);
        assertTrue(stream.snapshotIncludesFinish(32));
        assertEquals("重新回应。",stream.append("retry","重新回应。"));
        assertFalse(stream.snapshotIncludesFinish(33));
    }
}
