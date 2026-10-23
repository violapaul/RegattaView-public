#pragma once
// Demo: NMEA2000 library.
// This demo reads messages from NMEA 2000 bus and
// sends them translated to clear text to Serial.

// Note! If you use this on Arduino Mega, I prefer to also connect interrupt line
// for MCP2515 and define N2k_CAN_INT_PIN to related line. E.g. MessageSender
// sends so many messages that Mega can not handle them. If you only use
// received messages internally without slow operations, then youmay survive
// without interrupt.

#include "board.hpp"
#include "display.hpp"

#include <N2kMessages.h>
#include <N2kMessagesEnumToStr.h>
#include <NMEA2000_CAN.h>
#include <ArduinoJson.h>

#include "trueWind.hpp"

namespace nmea {

inline double mps2knots(double mps) {
    return mps * 1.94384;
}

inline double meters2feet(double meters) {
    return meters * 3.28084;
}

// Normalizes an angle in degrees to the range [0, 360)
inline double compassAngle(double deg) {
    deg = fmod(deg, 360);  // Use fmod to handle values larger than 360
    if (deg < 0)
        deg + 360;
    return deg;
}

// Converts radians to degrees
inline double rad2deg(double rad) {
    return compassAngle(rad * (180.0 / M_PI));
}

// Normalizes an angle in degrees to -180 to 180
inline double signedAngle(double deg) {
    deg = fmod(deg + 180, 360);  // Use fmod to handle values larger than 360
    if (deg < 0)
        deg += 360;
    return deg - 180;
}

typedef struct {
    unsigned long PGN;
    void (*Handler)(const tN2kMsg& N2kMsg);
} tNMEA2000Handler;

#define UN_INITIALIZED -999

typedef struct  // the state of the boat collected from the messages below
{
   private:
    double deltaTime = -1;

   public:
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

    float depth;
    float rudder;

    float twd;
    float twa;
    float tws;

    float ptwd = 10;
    float ptwa = 10;
    float ptws = 3;

    // magnetic variation
    double magVar;
    // double twa_sog;
    // double tws_sog;
    // Time is read from the 1Hz (?) system time message.  Between messages it is estimated.
    // Instead when we receive the time we'll store it minus the time received (millis()).
    // When we retrieve time we add back millis(), to keep up to date.
    void setTime(double time) {
        double cpuSeconds = millis() / 1000.0;
        // deltaTime is sort of the unixTime that the processor booted
        deltaTime = time - cpuSeconds;
    }
    void setTime(int days, double seconds) { setTime((double(days) * 24 * 60 * 60) + seconds); }
    double getTime() {
        if (deltaTime < 0) {
            return 0;
        }
        double cpuSeconds = millis() / 1000.0;
        return deltaTime + cpuSeconds;
    }
} tBoatState;

char boatStateChecksum(unsigned char* buf, int len) {
    // checksum is the XOR of all the bytes
    char checksum = 0;
    for (int i = 0; i < len; i++) {
        checksum ^= buf[i];
    }
    return checksum;
}

int boatStateEncode(unsigned char* buf, tBoatState& boatState) {
    LOG_DEBUG("Creating N2kMsg from boatState.");

    // The header is two bytes, the length followed by the checksum
    // Skip over the header
    int index = 2;
    // Note, index is incremented in each call to SetBuf based on the size of the data.
    SetBuf4ByteDouble(boatState.lat, 1e-7, index, buf);
    SetBuf4ByteDouble(boatState.lon, 1e-7, index, buf);

    SetBuf2ByteUDouble(boatState.sog, 1e-2, index, buf);
    SetBuf2ByteUDouble(boatState.cog, 1e-1, index, buf);

    SetBuf2ByteUDouble(boatState.hdg, 1e-1, index, buf);
    SetBuf2ByteUDouble(boatState.spd, 1e-2, index, buf);

    SetBuf2ByteUDouble(boatState.aws, 1e-2, index, buf);
    SetBuf2ByteDouble(boatState.awa, 1e-2, index, buf);

    SetBuf2ByteUDouble(boatState.saws, 1e-2, index, buf);
    SetBuf2ByteDouble(boatState.sawa, 1e-2, index, buf);

    SetBuf2ByteUDouble(boatState.depth, 1e-0, index, buf);
    SetBuf2ByteDouble(boatState.rudder, 1e-2, index, buf);

    SetBuf2ByteUDouble(boatState.twd, 1e-1, index, buf);
    SetBuf2ByteDouble(boatState.twa, 1e-2, index, buf);
    SetBuf2ByteUDouble(boatState.tws, 1e-2, index, buf);

    SetBuf2ByteUDouble(boatState.ptwd, 1e-1, index, buf);
    SetBuf2ByteDouble(boatState.ptwa, 1e-2, index, buf);
    SetBuf2ByteUDouble(boatState.ptws, 1e-2, index, buf);

    SetBuf2ByteDouble(boatState.magVar, 1e-2, index, buf);

    SetBuf8ByteDouble(boatState.getTime(), 1e-3, index, buf);
    // The checksum of all the bytes except the header
    buf[1] = boatStateChecksum(buf + 2, index);
    buf[0] = index;  // length of the message stored in first byte of the buf
    return index;
}

// Encode boat state as JSON into provided buffer (avoids heap allocation)
// Returns length of JSON string written
// stale: true if NMEA data is stale (no messages for >2 seconds)
size_t boatStateToJson(tBoatState& state, char* buffer, size_t bufferSize, bool stale) {
    JsonDocument doc;
    doc["t"] = state.getTime();
    doc["lat"] = state.lat;
    doc["lon"] = state.lon;
    doc["sog"] = round(state.sog * 100) / 100.0;
    doc["cog"] = round(state.cog * 10) / 10.0;
    doc["hdg"] = round(state.hdg * 10) / 10.0;
    doc["spd"] = round(state.spd * 100) / 100.0;
    doc["aws"] = round(state.aws * 10) / 10.0;
    doc["awa"] = round(state.awa * 10) / 10.0;
    doc["twd"] = round(state.twd * 10) / 10.0;
    doc["twa"] = round(state.twa * 10) / 10.0;
    doc["tws"] = round(state.tws * 10) / 10.0;
    doc["depth"] = round(state.depth * 10) / 10.0;
    doc["rudder"] = round(state.rudder * 10) / 10.0;
    doc["stale"] = stale;

    return serializeJson(doc, buffer, bufferSize);
}

Stream* OutputStream = &Serial;
Stream* LoggingStream = NULL;

tBoatState boatState;

void SystemTime(const tN2kMsg& N2kMsg);
void Rudder(const tN2kMsg& N2kMsg);
void Heading(const tN2kMsg& N2kMsg);
void RateOfTurn(const tN2kMsg& N2kMsg);
void Attitude(const tN2kMsg& N2kMsg);
void MagneticVariation(const tN2kMsg& N2kMsg);
void Speed(const tN2kMsg& N2kMsg);
void WaterDepth(const tN2kMsg& N2kMsg);
void LatLonRapid(const tN2kMsg& N2kMsg);
void COGSOG(const tN2kMsg& N2kMsg);
void GNSS(const tN2kMsg& N2kMsg);
void WindSpeed(const tN2kMsg& N2kMsg);
// void GNSSSatsInView(const tN2kMsg &N2kMsg);

// AIS handlers - stub functions to enable logging of AIS messages
void AISClassBPositionReport(const tN2kMsg& N2kMsg);
void AISClassBStaticDataA(const tN2kMsg& N2kMsg);
void AISClassBStaticDataB(const tN2kMsg& N2kMsg);

tNMEA2000Handler NMEA2000Handlers[] = {{126992L, &SystemTime},
                                       {127245L, &Rudder},
                                       {127250L, &Heading},
                                       {127251L, &RateOfTurn},
                                       {127257L, &Attitude},
                                       {127258L, &MagneticVariation},
                                       {128259L, &Speed},
                                       {128267L, &WaterDepth},
                                       {129025L, &LatLonRapid},
                                       {129026L, &COGSOG},
                                       {129029L, &GNSS},
                                       {130306L, &WindSpeed},
                                       // AIS PGNs - logged but not parsed
                                       {129039L, &AISClassBPositionReport},  // AIS Class B Position Report
                                       {129809L, &AISClassBStaticDataA},     // AIS Class B static data (msg 24 Part A)
                                       {129810L, &AISClassBStaticDataB},     // AIS Class B static data (msg 24 Part B)
                                       {0, 0}};

void HandleNMEA2000Msg(const tN2kMsg& N2kMsg);
void showMessageCounts(Stream* OutputStream);
// count messages of each type, same legth as NMEA2000Handlers
int pgnCounts[sizeof(NMEA2000Handlers) / sizeof(tNMEA2000Handler)] = {0};

//*****************************************************************************

// Consumes a callback that is called with every message
// typedef void (*MessageCallback)(const tN2kMsg& N2kMsg);
// MessageCallback *messageCallback;

void setup() {
    delay(500);
    LOG_INFO("Starting NMEA2000");
    LOG_INFO("Size of boatState:", sizeof(boatState));
    OutputStream = &Serial;

    NMEA2000.SetProductInformation("00000003",               // Manufacturer's Model serial code
                                   100,                      // Manufacturer's product code
                                   "Boat Brain v1",          // Manufacturer's Model ID
                                   "1.0.0.10 (2017-07-29)",  // Manufacturer's Software version code
                                   "1.0.0.0 (2017-07-12)"    // Manufacturer's Model version
    );

    // Set device information
    NMEA2000.SetDeviceInformation(
        2,    // Unique number. Use e.g. Serial number.
        130,  // Device function=Display. See codes on https://web.archive.org/web/20190531120557/https://www.nmea.org/Assets/20120726%20nmea%202000%20class%20&%20function%20codes%20v%202.00.pdf
        120,  // Device class=Display. See codes on  https://web.archive.org/web/20190531120557/https://www.nmea.org/Assets/20120726%20nmea%202000%20class%20&%20function%20codes%20v%202.00.pdf
        2046  // Just choosen free from code list on https://web.archive.org/web/20190529161431/http://www.nmea.org/Assets/20121020%20nmea%202000%20registration%20list.pdf
    );

    NMEA2000.SetN2kCANReceiveFrameBufSize(250);
    // Do not forward bus messages at all
    NMEA2000.SetForwardType(tNMEA2000::fwdt_Text);
    // NMEA2000.SetForwardStream(OutputStream);
    // Set false below, if you do not want to see messages parsed to HEX withing library
    NMEA2000.EnableForward(false);
    NMEA2000.SetMsgHandler(HandleNMEA2000Msg);
    //  NMEA2000.SetN2kCANMsgBufSize(2);
    bool ok = NMEA2000.Open();
    if (!ok) {
        LOG_ERROR("Failed to open NMEA2000");
    }

    OutputStream->print("Running...");
}

#define DATE_2030 21915
#define DATE_2020 18262
#define SEC_IN_DAY 86400
//*****************************************************************************
void SystemTime(const tN2kMsg& N2kMsg) {
    unsigned char SID;
    uint16_t SystemDate;  // days since 1.1.1970
    double SystemTime;    // seconds since midnight
    tN2kTimeSource TimeSource;

    if (ParseN2kSystemTime(N2kMsg, SID, SystemDate, SystemTime, TimeSource)) {
        // The date can be empty, but I don't see how this call can return this fact.  So we will
        // check for a reasonable date, before 2030, etc.
        if (N2kIsNA(SystemDate) || N2kIsNA(SystemTime)) {
            LOG_WARN("SystemDate is not valid.");
            return;
        }
        if (SystemDate < DATE_2030 && SystemDate > DATE_2020 && SystemTime < SEC_IN_DAY) {
            LOG_DEBUG("Setting time. Current Time", boatState.getTime());
            boatState.setTime(SystemDate, SystemTime);
            LOG_DEBUG("Setting time.     New Time", boatState.getTime());
        } else {
            LOG_WARN("Invalid SystemDate:", SystemDate);
            return;
        }
        // LOG_INFO("Receiving N2kSystemTime: %d %f\n", SystemDate, SystemTime);
    } else {
        LOG_WARN("Failed to parse PGN:", N2kMsg.PGN);
    }
}

#define SAWS_BASE_GAIN 0.01
#define SAWS_MAX_RESIDUAL 0.8
float sawsGain = SAWS_BASE_GAIN;

void smoothWindSpeed() {
    if (boatState.saws == UN_INITIALIZED) {
        boatState.saws = boatState.aws;
    } else {
        float residual = boatState.aws - boatState.saws;
        if (residual > SAWS_MAX_RESIDUAL) {
            sawsGain += SAWS_BASE_GAIN;
        } else {
            sawsGain = 0.7 * sawsGain + 0.3 * SAWS_BASE_GAIN;
        }
        boatState.saws = boatState.saws + sawsGain * residual;
    }
}

float angle_difference(float a1, float a2) {
    float da = a1 - a2;
    if (da > 180) {
        da = da - 360;
    } else if (da < -180) {
        da = da + 360;
    }
    return da;
}

#define SAWA_BASE_GAIN 0.1
#define SAWA_MAX_RESIDUAL 10.0
float sawaGain = SAWA_BASE_GAIN;

void smoothWindAngle() {
    if (boatState.sawa == UN_INITIALIZED) {
        boatState.sawa = boatState.awa;
    } else {
        float residual = angle_difference(boatState.awa, boatState.sawa);
        if (abs(residual) > SAWA_MAX_RESIDUAL) {
            sawaGain += SAWA_BASE_GAIN;
        } else {
            sawaGain = 0.7 * sawaGain + 0.3 * SAWA_BASE_GAIN;
        }
        boatState.sawa = signedAngle(boatState.sawa + sawaGain * residual);
    }
}

// *****************************************************************************
// wind speed
void WindSpeed(const tN2kMsg& N2kMsg) {
    unsigned char SID;
    double WindSpeed;
    double WindAngle;
    tN2kWindReference WindReference;

    LOG_DEBUG("WindSpeed MSG!");
    if (ParseN2kWindSpeed(N2kMsg, SID, WindSpeed, WindAngle, WindReference)) {
        // This is super tricky because the same PGN is used for different wind references.
        if (WindReference == N2kWind_Apparent) {
            boatState.awa = signedAngle(rad2deg(WindAngle));
            boatState.aws = mps2knots(WindSpeed);

            smoothWindSpeed();
            smoothWindAngle();
            trueWind::compute_update(boatState.awa, boatState.aws, boatState.cog, boatState.sog, boatState.hdg,
                                     boatState.ptwd, boatState.ptws);

            LOG_DEBUG("WindSpeed Apparent awa:", boatState.awa, " aws:", boatState.aws);
        } else if (WindReference == N2kWind_True_boat) {
            boatState.twa = signedAngle(rad2deg(WindAngle));
            boatState.tws = mps2knots(WindSpeed);
            LOG_DEBUG("WindSpeed True BOAT twa:", boatState.twa, " tws:", boatState.tws);
        } else if (WindReference == N2kWind_Magnetic) {
            // Note, we are using magnetic by popular demand.
            boatState.twd = compassAngle(rad2deg(WindAngle));
            boatState.tws = mps2knots(WindSpeed);
            LOG_DEBUG("WindSpeed Magnetic twd:", boatState.twd, " tws:", boatState.tws);
        } else if (WindReference == N2kWind_True_North) {
            boatState.twd = rad2deg(WindAngle);
            boatState.tws = mps2knots(WindSpeed);
            LOG_DEBUG("WindSpeed True NORTH twd:", boatState.twd, " tws:", boatState.tws);
        } else {
            LOG_WARN("WindSpeed reference not handled:", WindReference);
        }
    } else {
        LOG_WARN("Failed to parse PGN:", N2kMsg.PGN);
    }
}

// *****************************************************************************
// magnetic variation
void MagneticVariation(const tN2kMsg& N2kMsg) {
    unsigned char SID;
    tN2kMagneticVariation Source;
    uint16_t DaysSince1970;
    double Variation;

    if (ParseN2kMagneticVariation(N2kMsg, SID, Source, DaysSince1970, Variation)) {
        if (Variation != N2kDoubleNA) {
            boatState.magVar = signedAngle(rad2deg(Variation));
        }
    } else {
        LOG_WARN("Failed to parse PGN:", N2kMsg.PGN);
    }
}

// *****************************************************************************
// Latitude and Longitude rapid
void LatLonRapid(const tN2kMsg& N2kMsg) {
    double Latitude;
    double Longitude;

    if (ParseN2kPGN129025(N2kMsg, Latitude, Longitude)) {
        boatState.lat = Latitude;
        boatState.lon = Longitude;
    } else {
        LOG_WARN("Failed to parse PGN:", N2kMsg.PGN);
    }
}

// *****************************************************************************
// rate of turn
void RateOfTurn(const tN2kMsg& N2kMsg) {
    unsigned char SID;
    double RateOfTurn;

    // WARNING: skipping this for now
    // if (ParseN2kRateOfTurn(N2kMsg, SID, RateOfTurn))
    // {
    //   // boatState.hdgRate = RateOfTurn;
    // }
    // else
    // {
    //   OutputStream->print("Failed to parse PGN: ");
    //   OutputStream->println(N2kMsg.PGN);
    // }
}

//*****************************************************************************
void Rudder(const tN2kMsg& N2kMsg) {
    unsigned char Instance;
    tN2kRudderDirectionOrder RudderDirectionOrder;
    double RudderPosition;
    double AngleOrder;

    if (ParseN2kRudder(N2kMsg, RudderPosition, Instance, RudderDirectionOrder, AngleOrder)) {
        boatState.rudder = signedAngle(rad2deg(RudderPosition));
    } else {
        LOG_WARN("Failed to parse PGN:", N2kMsg.PGN);
    }
}

//*****************************************************************************
void Heading(const tN2kMsg& N2kMsg) {
    unsigned char SID;
    tN2kHeadingReference HeadingReference;
    double Heading;
    double Deviation;
    double Variation;

    // There are two compasses on the boat and they are not in agreement.
    // Have check the source of the message to determine which compass to use.
    if (N2kMsg.Source != 4) {
        return;
    }

    if (ParseN2kHeading(N2kMsg, SID, Heading, Deviation, Variation, HeadingReference)) {
        if (!N2kIsNA(Variation)) {
            boatState.magVar = signedAngle(rad2deg(Variation));
        }
        if (!N2kIsNA(Heading)) {
            if (HeadingReference == N2khr_magnetic) {
                boatState.hdg = rad2deg(Heading);
                // boatState.hdg = compassAngle(rad2deg(Heading) + boatState.magVar);
            } else if (HeadingReference == N2khr_true) {
                boatState.hdg = rad2deg(Heading);
            } else {
                LOG_WARN("Heading reference not handled:", HeadingReference);
            }
            trueWind::compute_update(boatState.awa, boatState.aws, boatState.cog, boatState.sog, boatState.hdg,
                                     boatState.ptwd, boatState.ptws);
        }
    } else {
        LOG_WARN("Failed to parse PGN:", N2kMsg.PGN);
    }
}

//*****************************************************************************
void COGSOG(const tN2kMsg& N2kMsg) {
    unsigned char SID;
    tN2kHeadingReference HeadingReference;
    double COG;
    double SOG;

    if (ParseN2kCOGSOGRapid(N2kMsg, SID, HeadingReference, COG, SOG)) {
        boatState.sog = mps2knots(SOG);
        boatState.cog = rad2deg(COG);
        trueWind::compute_update(boatState.awa, boatState.aws, boatState.cog, boatState.sog, boatState.hdg,
                                 boatState.ptwd, boatState.ptws);

    } else {
        OutputStream->print("Failed to parse PGN: ");
        OutputStream->println(N2kMsg.PGN);
    }
}

//*****************************************************************************
void GNSS(const tN2kMsg& N2kMsg) {
    unsigned char SID;
    uint16_t DaysSince1970;
    double SecondsSinceMidnight;
    double Latitude;
    double Longitude;
    double Altitude;
    tN2kGNSStype GNSStype;
    tN2kGNSSmethod GNSSmethod;
    unsigned char nSatellites;
    double HDOP;
    double PDOP;
    double GeoidalSeparation;
    unsigned char nReferenceStations;
    tN2kGNSStype ReferenceStationType;
    uint16_t ReferenceSationID;
    double AgeOfCorrection;

    if (ParseN2kGNSS(N2kMsg, SID, DaysSince1970, SecondsSinceMidnight, Latitude, Longitude, Altitude, GNSStype,
                     GNSSmethod, nSatellites, HDOP, PDOP, GeoidalSeparation, nReferenceStations, ReferenceStationType,
                     ReferenceSationID, AgeOfCorrection)) {
        // Position is intentionally ignored here -- the fast 129025 LatLonRapid handler owns
        // lat/lon. But GNSS also carries date+time, so set the clock from it as a second source.
        // Without this the clock could only be set by PGN 126992 (SystemTime); a GPS that emits
        // 129029 (GNSS) but not 126992 would never set the clock, and the logger would stay in the
        // 000NNN.log fallback naming. Same validity gate as SystemTime().
        if (!N2kIsNA(DaysSince1970) && !N2kIsNA(SecondsSinceMidnight) &&
            DaysSince1970 < DATE_2030 && DaysSince1970 > DATE_2020 && SecondsSinceMidnight < SEC_IN_DAY) {
            boatState.setTime(DaysSince1970, SecondsSinceMidnight);
        }
    } else {
        OutputStream->print("Failed to parse PGN: ");
        OutputStream->println(N2kMsg.PGN);
    }
}

//*****************************************************************************
void Speed(const tN2kMsg& N2kMsg) {
    unsigned char SID;
    double SOW;
    double SOG;
    tN2kSpeedWaterReferenceType SWRT;

    if (ParseN2kBoatSpeed(N2kMsg, SID, SOW, SOG, SWRT)) {
        boatState.spd = mps2knots(SOW);
    }
}

//*****************************************************************************
void WaterDepth(const tN2kMsg& N2kMsg) {
    unsigned char SID;
    double DepthBelowTransducer;
    double Offset;

    if (ParseN2kWaterDepth(N2kMsg, SID, DepthBelowTransducer, Offset)) {
        boatState.depth = DepthBelowTransducer;
        if (!N2kIsNA(Offset)) {
            boatState.depth += Offset;
        }
        boatState.depth = meters2feet(boatState.depth);
    }
}

//*****************************************************************************
void Attitude(const tN2kMsg& N2kMsg) {
    unsigned char SID;
    double Yaw;
    double Pitch;
    double Roll;

    // WARNING ignoring for now.
    // if (ParseN2kAttitude(N2kMsg, SID, Yaw, Pitch, Roll))
    // {
    //   OutputStream->println("Attitude:");
    //   PrintLabelValWithConversionCheckUnDef("  SID: ", SID, 0, true);
    //   PrintLabelValWithConversionCheckUnDef("  Yaw (deg): ", Yaw, &RadToDeg, true);
    //   PrintLabelValWithConversionCheckUnDef("  Pitch (deg): ", Pitch, &RadToDeg, true);
    //   PrintLabelValWithConversionCheckUnDef("  Roll (deg): ", Roll, &RadToDeg, true);
    // }
    // else
    // {
    //   OutputStream->print("Failed to parse PGN: ");
    //   OutputStream->println(N2kMsg.PGN);
    // }
}

//*****************************************************************************
// AIS stub handlers - these do nothing but allow the messages to be logged
// The actual parsing happens in the Python pipeline after the fact
void AISClassBPositionReport(const tN2kMsg& N2kMsg) {
    // PGN 129039: Contains MMSI, lat, lon, COG, SOG, heading
    // Logged automatically by HandleNMEA2000Msg, no parsing needed here
}

void AISClassBStaticDataA(const tN2kMsg& N2kMsg) {
    // PGN 129809: Contains MMSI, vessel name (Part A)
    // Logged automatically by HandleNMEA2000Msg, no parsing needed here
}

void AISClassBStaticDataB(const tN2kMsg& N2kMsg) {
    // PGN 129810: Contains MMSI, call sign, vessel type, dimensions (Part B)
    // Logged automatically by HandleNMEA2000Msg, no parsing needed here
}

// By default system time messages are only sent 1x a second.  The messages themselves do not
// contain time info. Code below is designed to add additional system time messages regularly so
// that the reader of the stream can more accurately determine the arrival time of the messagse.

#define SYSTEM_TIME_INTERVAL 100
unsigned long lastExtraSystemTimeMessage = 0;
double firstExtraSystemTime = 0;

void sendExtraSystemTimeMessage() {
    // LoggingStream can be cleared from another task (e.g. a graceful shutdown
    // sets it NULL mid-loop); the caller's `if (LoggingStream)` check is a
    // separate task's TOCTOU window, so re-check here before dereferencing.
    if (LoggingStream == NULL) return;
    double nmeaTime = boatState.getTime();
    unsigned long bbTime = millis();
    // The time is currently valid.
    if (nmeaTime > 0 && bbTime > lastExtraSystemTimeMessage + SYSTEM_TIME_INTERVAL) {
        if (firstExtraSystemTime == 0) {
            firstExtraSystemTime = nmeaTime - (bbTime / 1000.0);
        }
        lastExtraSystemTimeMessage = bbTime;
        double bbDateTime = firstExtraSystemTime + (bbTime / 1000.0);
        tN2kMsg msg;
        // Set sender
        msg.Source = 99;  // Trying to pick a random source
        // days since epoch
        int days = floor(bbDateTime / (24 * 60 * 60));
        // seconds since midnight
        double seconds = bbDateTime - days * 24 * 60 * 60;
        SetN2kSystemTime(msg, 0, days, seconds);
        msg.SendInActisenseFormat(LoggingStream);
        LoggingStream->println();
    }
}

long MESSAGE_COUNT = 1;
unsigned long lastMessageTime = 0;

//*****************************************************************************
// NMEA 2000 message handler
void HandleNMEA2000Msg(const tN2kMsg& msg) {
    int iHandler;
    // Find handler
    // OutputStream->print("In Main Handler: ");
    // OutputStream->println(N2kMsg.PGN);
    for (iHandler = 0; NMEA2000Handlers[iHandler].PGN != 0 && !(msg.PGN == NMEA2000Handlers[iHandler].PGN); iHandler++)
        ;

    if (NMEA2000Handlers[iHandler].PGN != 0) {
        NMEA2000Handlers[iHandler].Handler(msg);
        // If the logging stream exists then log the message
        if (LoggingStream) {
            // write the message to the logging stream
            msg.SendInActisenseFormat(LoggingStream);
            LoggingStream->println();
            sendExtraSystemTimeMessage();
        }
        // update the count
        pgnCounts[iHandler] += 1;
        MESSAGE_COUNT += 1;
        lastMessageTime = millis();
    }
}

bool messageStall(unsigned long maxWaitTime) {
    return millis() > lastMessageTime + maxWaitTime;
}

// Legacy, we explicitly encode this now.
// send the binary boatstate to the logging stream
// void sendBoatState(Stream* stream) {
//     // write the message to the logging stream
//     stream->write((uint8_t*)&boatState, sizeof(boatState));
// }

long LOOP_COUNT = 0;
void printState(Stream* stream) {
    stream->printf("Loop: %9d", LOOP_COUNT);
    stream->printf(" Msg: %6d", MESSAGE_COUNT);
    stream->print(" lat: ");
    // stream->print(boatState.lat, 6);
    // printf with 3 before and 6 after the decimal
    stream->printf("%9.6f", boatState.lat);
    stream->print(" lon: ");
    stream->printf("%9.6f", boatState.lon);
    stream->print(" sog: ");
    stream->printf("%6.2f", boatState.sog);
    stream->print(" cog: ");
    stream->printf("%6.2f", boatState.cog);
    stream->print(" hdg: ");
    stream->printf("%6.2f", boatState.hdg);
    stream->print(" spd: ");
    stream->printf("%6.2f", boatState.spd);
    stream->print(" aws: ");
    stream->printf("%6.2f", boatState.aws);
    stream->print(" awa: ");
    stream->printf("%6.2f", boatState.awa);
    stream->print(" depth: ");
    stream->printf("%6.2f", boatState.depth);
    stream->print(" rudder: ");
    stream->printf("%6.2f", boatState.rudder);
    stream->print(" twd: ");
    stream->printf("%6.2f", boatState.twd);
    stream->print(" twa: ");
    stream->printf("%6.2f", boatState.twa);
    stream->print(" tws: ");
    stream->printf("%6.2f", boatState.tws);
    stream->print(" magVar: ");
    stream->printf("%6.2f", boatState.magVar);
    stream->print(" unixTime: ");
    stream->println(boatState.getTime(), 2);
}

void showMessageCounts(Stream* OutputStream) {
    for (int iHandler = 0; NMEA2000Handlers[iHandler].PGN != 0; iHandler++) {
        if (pgnCounts[iHandler] > 0) {
            OutputStream->print("PGN: ");
            OutputStream->print(NMEA2000Handlers[iHandler].PGN);
            OutputStream->print(" Count: ");
            OutputStream->println(pgnCounts[iHandler]);
        }
    }
}

unsigned long lastStateTime = 0;
//*****************************************************************************
// Note this is really not a loop!  Its called repeatedly from a task.
void loopStep() {
    if (LOOP_COUNT == 0) {
        // print each PGN with the message count
        LOG_INFO("Before first call to parse messages");
    }
    LOOP_COUNT++;

    NMEA2000.ParseMessages();
    // freertos delay
    // vTaskDelay(1);
    // every 100 messages print the state, careful with float to print 2 digits

    if (millis() > lastStateTime + 1000) {
        printState(OutputStream);
        lastStateTime = millis();
    }
}
// Test mode - generates simulated sailing data
bool testModeEnabled = false;

void enableTestMode(bool enable) {
    testModeEnabled = enable;
    if (enable) {
        LOG_INFO("TEST MODE ENABLED - generating simulated data");
        // Prevent initial stall detection
        lastMessageTime = millis();
        // Initialize with reasonable sailing values
        boatState.lat = 47.6062;  // Seattle area
        boatState.lon = -122.3321;
        boatState.sog = 5.5;
        boatState.cog = 270;
        boatState.hdg = 275;
        boatState.spd = 5.2;
        boatState.aws = 12.0;
        boatState.awa = -35;
        boatState.tws = 10.0;
        boatState.twa = -42;
        boatState.twd = 233;
        boatState.depth = 45.0;
        boatState.rudder = 2.0;
        boatState.magVar = 15.5;
        // Set a fake time (current CPU time as seconds since epoch approximation)
        boatState.setTime(1713100000.0);  // ~April 2024
    }
}

// Generate smoothly varying test data - call at 4Hz
void generateTestData() {
    if (!testModeEnabled) return;

    static float phase = 0;
    phase += 0.02;  // Slow phase advance

    // Simulate gentle variations
    float windShift = sin(phase * 0.3) * 5;  // +/- 5 degree wind shifts
    float gustFactor = 1.0 + sin(phase * 1.5) * 0.15;  // +/- 15% gusts

    // Update wind
    boatState.twd = compassAngle(233 + windShift);
    boatState.tws = 10.0 * gustFactor;
    boatState.aws = 12.0 * gustFactor;
    boatState.awa = -35 + sin(phase * 0.5) * 3;  // Small AWA oscillation
    boatState.twa = -42 + windShift;

    // Update boat motion
    boatState.sog = 5.5 + sin(phase * 0.7) * 0.5;  // Speed varies with waves
    boatState.spd = boatState.sog - 0.3 + sin(phase * 0.8) * 0.2;  // Boat speed slightly different
    boatState.cog = compassAngle(270 + sin(phase * 0.2) * 3);  // Slight course wander
    boatState.hdg = compassAngle(boatState.cog + 5 + sin(phase * 0.4) * 2);  // Heading vs COG

    // Rudder oscillates as helmsman steers
    boatState.rudder = sin(phase * 2) * 5;  // +/- 5 degrees

    // Depth varies slowly (simulating sailing over varying bottom)
    boatState.depth = 45 + sin(phase * 0.1) * 10;

    // Move position slowly
    boatState.lat += 0.00001 * sin(boatState.cog * M_PI / 180);
    boatState.lon += 0.00001 * cos(boatState.cog * M_PI / 180);

    // Advance time
    double currentTime = boatState.getTime();
    if (currentTime > 0) {
        boatState.setTime(currentTime + 0.25);  // Add 250ms
    }

    // Update lastMessageTime to prevent stall detection
    lastMessageTime = millis();
}

bool isTestMode() {
    return testModeEnabled;
}

}  // namespace nmea
