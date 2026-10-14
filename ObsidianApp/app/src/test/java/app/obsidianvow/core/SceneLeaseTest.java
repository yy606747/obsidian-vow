package app.obsidianvow.core;

import org.junit.Test;
import static org.junit.Assert.*;

public class SceneLeaseTest {
    private long now = 1000;
    private final SceneLease lease = new SceneLease(() -> now);

    @Test public void expiredLeaseCannotBeRenewedByDelayedResponse() {
        String token = lease.open("scene", 1);
        long request = lease.request(token);
        now += 3001;
        assertFalse(lease.alive(token));
        assertFalse(lease.renew(token, request));
        assertFalse(lease.frame(token, "muse_ble", 1));
    }

    @Test public void localRequestTimeBoundsLeaseAndResponseIsSingleUse() {
        String token = lease.open("scene", 1);
        long request = lease.request(token);
        now += 1400;
        assertTrue(lease.renew(token, request));
        assertFalse(lease.renew(token, request));
        now = 4001;
        assertFalse(lease.alive(token)); // Not 3 seconds after response arrival.
    }

    @Test public void oldRequestCannotReplaceTheNewerRequest() {
        String token = lease.open("scene", 1);
        long old = lease.request(token), latest = lease.request(token);
        assertFalse(lease.renew(token, old));
        assertTrue(lease.renew(token, latest));
    }

    @Test public void slowResponsesFailClosedEvenBeforeLeaseDeadline() {
        String token = lease.open("scene", 1);
        long request = lease.request(token);
        now += 1501;
        assertFalse(lease.renew(token, request));
    }

    @Test public void pauseAndEndFenceSameOrOlderGenerationButAllowExplicitResume() {
        String token = lease.open("scene", 2);
        long request = lease.request(token);
        lease.close();
        assertFalse(lease.renew(token, request));
        assertEquals("", lease.open("scene", 2));
        assertEquals("", lease.open("scene", 1));
        String resumed = lease.open("scene", 4);
        assertFalse(resumed.isEmpty());
        assertNotEquals(token, resumed);
        assertFalse(lease.alive(token));
        assertEquals("", lease.open("scene", 4));
    }

    @Test public void devicesHaveIndependentFrameFences() {
        String token = lease.open("scene", 1);
        assertTrue(lease.frame(token, "muse_ble", 3));
        assertTrue(lease.frame(token, "sosexy_ble", 7));
        assertTrue(lease.permits(token, "muse_ble", 3));
        assertTrue(lease.frame(token, "sosexy_ble", 8));
        assertFalse(lease.permits(token, "sosexy_ble", 7));
        assertFalse(lease.frame(token, "sosexy_ble", 6));
        assertTrue(lease.permits(token, "muse_ble", 3));
        lease.close();
        assertFalse(lease.permits(token, "muse_ble", 3));
    }
}
