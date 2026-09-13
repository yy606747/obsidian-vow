package app.obsidianvow.core

import org.junit.Assert.*
import org.junit.Test

class LookAtMeSessionTest {
    private fun session(): LookAtMeSession = LookAtMeSession().apply {
        id="request";convId="today";seconds=20;expires=120000
    }
    @Test fun notificationDoesNotAcceptAndFirstShowHasItsOwnDeadline() {
        val s=session();s.notified=true
        assertFalse(s.accepted());assertFalse(s.resident())
        assertTrue(s.offer(119000));assertEquals(239000,s.choiceDeadline)
        assertTrue(s.offer(200000));assertEquals(239000,s.choiceDeadline)
        assertFalse(s.decide("accepted",239000))
        s.finish("deadline",239000);assertEquals("timed_out",s.decision);assertEquals("",s.outcome)
    }
    @Test fun expiredUnopenedNotificationHasNoDecision() {
        val s=session();s.notified=true
        assertFalse(s.offer(120000));s.finish("deadline",120000)
        assertEquals("",s.decision);assertEquals("",s.outcome)
    }
    @Test fun delayedResponseCannotBackdateAnExpiredInvitation() {
        val s=session()
        // Server generates at 119 s, but transport finishes at 129 s.
        s.anchorResponse(119000,120000,119000,129000)
        assertEquals(129000,s.wall(129000))
        assertFalse(s.offer(129000))
        s.finish("deadline",129000)
        assertEquals("",s.decision);assertEquals("",s.outcome)
    }
    @Test fun roundTripAndMainQueueTimeAreBothChargedWithoutMovingTheChoiceTimer() {
        val s=session()
        s.anchorResponse(110000,120000,100000,112000)
        assertEquals(122000,s.wall(112000))
        assertFalse(s.offer(112000)) // includes time spent long-polling
        val queued=session()
        queued.anchorResponse(100000,120000,100000,101000)
        assertFalse(queued.offer(120000)) // main-thread dispatch cannot renew it
        val fast=session();fast.anchorResponse(100000,120000,100000,101000)
        assertTrue(fast.offer(102000))
        assertEquals(222000,fast.choiceDeadline)
        assertTrue(fast.offer(121000));assertEquals(222000,fast.choiceDeadline)
    }
    @Test fun midnightReopensAcceptedChatWithoutExtendingTheStartDeadline() {
        val s=session();s.offer(100);s.decide("accepted",200)
        assertFalse(s.ready("request","tomorrow","today",true,300))
        assertTrue(s.followDaily("tomorrow")) // controller must request another handshake
        assertEquals(200,s.acceptedAt)
        assertFalse(s.followDaily("tomorrow"))
        assertFalse(s.startExpired(15199))
        assertTrue(s.ready("request","tomorrow","tomorrow",true,15199))
        val end=s.end
        assertTrue(s.followDaily("day_after"));assertEquals(end,s.end)
        val late=session();late.offer(100);late.decide("accepted",200)
        assertTrue(late.followDaily("tomorrow"));assertTrue(late.startExpired(15200))
        assertFalse(late.ready("request","tomorrow","tomorrow",true,15200))
    }
    @Test fun realVisualHandshakeStartsExactlyOnceAndMidnightDoesNotExtend() {
        val s=session();s.offer(100);s.decide("accepted",200)
        assertFalse(s.ready("wrong","today","today",true,300))
        assertFalse(s.ready("request","history","today",true,300))
        assertFalse(s.ready("request","today","today",false,300))
        assertTrue(s.ready("request","today","today",true,300))
        assertEquals(20300,s.end)
        assertFalse(s.ready("request","tomorrow","tomorrow",true,1000))
        assertEquals(20300,s.end)
        s.finish("deadline",20500);assertEquals("completed",s.outcome);assertEquals(20000,s.actualMs)
    }
    @Test fun emergencyOfflineEndAndRestartNeverResumeOrInventElapsedTime() {
        for(reason in listOf("manual","screen_off","call","return_failed","disabled")) {
            val s=session();s.offer(100);s.decide("accepted",200);s.ready("request","today","today",true,300)
            s.finish(reason,5000)
            assertFalse(s.live());assertEquals("ended_early",s.outcome);assertEquals(4700,s.actualMs)
            assertFalse(s.ready("request","today","today",true,6000))
        }
        val s=session();s.offer(100);s.decide("accepted",200);s.ready("request","today","today",true,300)
        s.observe(4000);s.recover();assertEquals(3700,s.actualMs);assertEquals("ended_early",s.outcome)
        assertFalse(s.live())
    }
    @Test fun rejectionAndFailedStartDoNotHaveSessionOutcomes() {
        val s=session();s.offer(100);s.decide("rejected",200)
        assertEquals("rejected",s.decision);assertEquals("",s.outcome)
        val accepted=session();accepted.offer(100);accepted.decide("accepted",200);accepted.recover()
        assertEquals("accepted",accepted.decision);assertEquals("",accepted.outcome);assertFalse(accepted.live())
    }
}
