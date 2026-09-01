package app.obsidianvow.core;

import java.util.Arrays;

final class MuseBleEncoder {
    private static final byte[] PREFIX = new byte[]{0x77, 0x62, 0x4d, 0x53, 0x45};

    private MuseBleEncoder() {}

    static byte[] encodeCommand(int command) {
        return encodeRfPayload(PREFIX, new byte[]{(byte) (command & 0xFF)});
    }

    private static byte[] encodeRfPayload(byte[] prefix, byte[] payload) {
        int payloadStart = 18 + prefix.length;
        int crcStart = payloadStart + payload.length;
        byte[] frame = new byte[15 + 3 + prefix.length + payload.length + 2];
        frame[15] = 0x71;
        frame[16] = 0x0f;
        frame[17] = 0x55;
        for (int i = 0; i < prefix.length; i++) {
            frame[18 + i] = prefix[prefix.length - 1 - i];
        }
        System.arraycopy(payload, 0, frame, payloadStart, payload.length);

        for (int i = 15; i < 18 + prefix.length; i++) {
            frame[i] = (byte) invert8(frame[i] & 0xFF);
        }

        int checksum = crc16(prefix, payload);
        frame[crcStart] = (byte) (checksum & 0xFF);
        frame[crcStart + 1] = (byte) ((checksum >> 8) & 0xFF);

        byte[] inner = Arrays.copyOfRange(frame, 18, crcStart + 2);
        whiteningEncode(inner, 0x3f);
        System.arraycopy(inner, 0, frame, 18, inner.length);
        whiteningEncode(frame, 0x25);
        return Arrays.copyOfRange(frame, 15, frame.length);
    }

    private static int crc16(byte[] addr, byte[] payload) {
        int crc = 0xffff;
        for (int i = addr.length - 1; i >= 0; i--) {
            crc = crcFeed(crc, addr[i] & 0xFF);
        }
        for (byte value : payload) {
            crc = crcFeed(crc, invert8(value & 0xFF));
        }
        return (~invert16(crc)) & 0xffff;
    }

    private static int crcFeed(int crc, int value) {
        crc ^= (value & 0xFF) << 8;
        for (int i = 0; i < 8; i++) {
            if ((crc & 0x8000) != 0) crc = ((crc << 1) ^ 0x1021) & 0xffff;
            else crc = (crc << 1) & 0xffff;
        }
        return crc & 0xffff;
    }

    private static int invert8(int value) {
        int result = 0;
        for (int bit = 0; bit < 8; bit++) {
            if ((value & (1 << bit)) != 0) result |= 1 << (7 - bit);
        }
        return result & 0xFF;
    }

    private static int invert16(int value) {
        int result = 0;
        value &= 0xffff;
        for (int bit = 0; bit < 16; bit++) {
            if ((value & (1 << bit)) != 0) result |= 1 << (15 - bit);
        }
        return result & 0xffff;
    }

    private static void whiteningEncode(byte[] data, int channel) {
        int[] state = whiteningInit(channel);
        for (int i = 0; i < data.length; i++) {
            int encoded = data[i] & 0xFF;
            for (int bit = 0; bit < 8; bit++) {
                if (whiteningOutput(state) != 0) encoded ^= 1 << bit;
            }
            data[i] = (byte) (encoded & 0xFF);
        }
    }

    private static int[] whiteningInit(int channel) {
        return new int[]{
                1,
                (channel >> 5) & 1,
                (channel >> 4) & 1,
                (channel >> 3) & 1,
                (channel >> 2) & 1,
                (channel >> 1) & 1,
                channel & 1,
        };
    }

    private static int whiteningOutput(int[] state) {
        int result = state[6];
        int s0 = state[6];
        int s1 = state[0];
        int s2 = state[1];
        int s3 = state[2];
        int s4 = state[6] ^ state[3];
        int s5 = state[4];
        int s6 = state[5];
        state[0] = s0;
        state[1] = s1;
        state[2] = s2;
        state[3] = s3;
        state[4] = s4;
        state[5] = s5;
        state[6] = s6;
        return result;
    }
}
