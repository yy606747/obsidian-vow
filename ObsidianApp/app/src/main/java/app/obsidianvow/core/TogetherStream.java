package app.obsidianvow.core;

/** Presentation state spanning long-poll deltas and independently fetched snapshots. */
final class TogetherStream {
    private String text="",turn="";
    private long finishedAt;

    String append(String turnId,String delta){
        if(!turn.equals(turnId)){text="";turn=turnId;}
        finishedAt=0;text+=delta;
        if(text.length()>32000)text=text.substring(text.length()-32000);
        return text;
    }
    void finish(long cursor){text="";finishedAt=cursor;}
    boolean snapshotIncludesFinish(long cursor){
        if(finishedAt<=0||cursor<finishedAt)return false;
        finishedAt=0;return true;
    }
}
