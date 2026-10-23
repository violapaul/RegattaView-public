#pragma once

#include <Arduino.h>
#include "display.hpp"

#define UN_INITIALIZED -999

namespace windex {

#include "msgEncode.hpp"

using namespace msgEncode;

typedef struct  // windex data, raw and processed
{
   private:
    double deltaTime = -1;
   public:
    // These are the raw sensor values
    // The raw window angle rotary sensor.
    float rawAwaAngle = UN_INITIALIZED;
    float rawAwaSpeed = UN_INITIALIZED;

    // The raw wind speed rotary sensor.
    float rawAwsAngle = UN_INITIALIZED;
    float rawAwsSpeed = UN_INITIALIZED;

    // The raw IMU orientation sensor.
    float roll = UN_INITIALIZED;
    float pitch = UN_INITIALIZED;
    float yaw = UN_INITIALIZED;

    // by convention all angles are in degrees, all speeds are in m/s
    double lat;
    double lon;

    float sog;
    float cog;

    float hdg;
    float spd;

    float aws;
    float awa;
    float saws = UN_INITIALIZED;
    float sawa = UN_INITIALIZED;

    float twd;
    float twa;
    float tws;

    float ptwd = 10;
    float ptwa = 10;
    float ptws = 3;

    double magVar;
} tWindexState;

char msgChecksum(unsigned char* buf, int len) {
    // checksum is the XOR of all the bytes
    char checksum = 0;
    for (int i = 0; i < len; i++) {
        checksum ^= buf[i];
    }
    return checksum;
}

int windexEncode(unsigned char* buf, tWindexState &state) {
    LOG_DEBUG("Creating windex message.");

    // The header is two bytes, the length followed by the checksum
    // Skip over the header
    int index = 2;
    // Note, index is incremented in each call to SetBuf based on the size of the data.
    SetBuf2ByteDouble(state.rawAwaAngle, 1e-1, index, buf);
    SetBuf2ByteDouble(state.rawAwaSpeed, 1e-1, index, buf);

    SetBuf2ByteDouble(state.rawAwsAngle, 1e-1, index, buf);
    SetBuf2ByteDouble(state.rawAwsSpeed, 1e-1, index, buf);

    SetBuf2ByteDouble(state.roll, 1e-2, index, buf);
    SetBuf2ByteDouble(state.pitch, 1e-2, index, buf);
    SetBuf2ByteDouble(state.yaw, 1e-2, index, buf);

    SetBuf4ByteDouble(state.lat, 1e-7, index, buf);
    SetBuf4ByteDouble(state.lon, 1e-7, index, buf);

    SetBuf2ByteUDouble(state.sog, 1e-2, index, buf);
    SetBuf2ByteUDouble(state.cog, 1e-2, index, buf);

    SetBuf2ByteDouble(state.hdg, 1e-2, index, buf);
    SetBuf2ByteUDouble(state.spd, 1e-2, index, buf);

    SetBuf2ByteUDouble(state.aws, 1e-2, index, buf);
    SetBuf2ByteDouble(state.awa, 1e-2, index, buf);

    SetBuf2ByteUDouble(state.saws, 1e-2, index, buf);
    SetBuf2ByteDouble(state.sawa, 1e-2, index, buf);

    SetBuf2ByteDouble(state.twd, 1e-2, index, buf);
    SetBuf2ByteDouble(state.twa, 1e-2, index, buf);
    SetBuf2ByteUDouble(state.tws, 1e-2, index, buf);

    SetBuf2ByteDouble(state.ptwd, 1e-2, index, buf);
    SetBuf2ByteDouble(state.ptwa, 1e-2, index, buf);
    SetBuf2ByteUDouble(state.ptws, 1e-2, index, buf);

    SetBuf2ByteDouble(state.magVar, 1e-2, index, buf);

    // SetBuf8ByteDouble(state.getTime(), 1e-3, index, buf);
    // The checksum of all the bytes except the header
    buf[1] = msgChecksum(buf+2, index);
    buf[0] = index;  // length of the message stored in first byte of the buf
    return index;

}

}