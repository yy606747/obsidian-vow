package app.obsidianvow.core;

import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/** Completed visible replies only. Initial snapshots and repeated syncs stay quiet. */
final class TogetherReplies {
    static final class Message {
        final String id, role, status, text;
        Message(String id, String role, String status, String text) {
            this.id=id; this.role=role; this.status=status; this.text=text;
        }
    }
    private final Set<String> completed=new LinkedHashSet<>();
    private boolean primed;

    String observe(List<Message> messages) {
        String newest="";
        for(Message message:messages) {
            if(!"assistant".equals(message.role)||!"complete".equals(message.status)
                    ||message.id.isEmpty()||message.text.trim().isEmpty())continue;
            if(completed.add(message.id)&&primed)newest=message.text;
        }
        primed=true;
        // The server sends a bounded recent history, far below this size.
        while(completed.size()>2048)completed.remove(completed.iterator().next());
        return newest;
    }
}
