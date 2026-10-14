package app.obsidianvow.core;

import org.junit.Test;
import java.util.Arrays;
import java.util.Collections;
import static org.junit.Assert.*;

public class TogetherRepliesTest {
    private TogetherReplies.Message message(String id,String role,String status,String text){
        return new TogetherReplies.Message(id,role,status,text);
    }
    @Test public void initialHistoryAndReconnectDoNotRepeatAlerts(){
        TogetherReplies tracker=new TogetherReplies();
        TogetherReplies.Message old=message("a","assistant","complete","old reply");
        assertEquals("",tracker.observe(Arrays.asList(old)));
        TogetherReplies.Message next=message("b","assistant","complete","new reply");
        assertEquals("new reply",tracker.observe(Arrays.asList(old,next)));
        assertEquals("",tracker.observe(Arrays.asList(old,next)));
        assertEquals("",tracker.observe(Arrays.asList(next)));
        assertEquals("",tracker.observe(Arrays.asList(old,next)));
    }
    @Test public void OwnerStatusSilentTurnsAndPartialRepliesDoNotAlert(){
        TogetherReplies tracker=new TogetherReplies();tracker.observe(Collections.emptyList());
        assertEquals("",tracker.observe(Arrays.asList(
            message("u","user","complete","input"),
            message("a","assistant","interrupted","unfinished"),
            message("b","assistant","running","streamed text"),
            message("c","assistant","complete","  "))));
        assertEquals("finished",tracker.observe(Arrays.asList(message("b","assistant","complete","finished"))));
    }
    @Test public void SeveralRepliesInOneSyncUseLatestPreviewAndAreAcknowledgedTogether(){
        TogetherReplies tracker=new TogetherReplies();tracker.observe(Collections.emptyList());
        TogetherReplies.Message first=message("a","assistant","complete","one");
        TogetherReplies.Message second=message("b","assistant","complete","two");
        assertEquals("two",tracker.observe(Arrays.asList(first,second)));
        assertEquals("",tracker.observe(Arrays.asList(first,second)));
    }
}
