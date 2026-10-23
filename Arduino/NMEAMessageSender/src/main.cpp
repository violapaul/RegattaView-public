// Demo: NMEA2000 library. Send different NMEA 2000 messages as example to the bus.
//
// MessageSender uses tN2kSyncScheduler, which sends all messages synchronized to library open time.
// As default messages will have their default(NMEA2000 specified) period. Messages also
// has different offsets to avoid filling up buffer at once. NMEA2000 bus can handle about
// 2.5 frame/ms. So two single frame messages could be sent at same time, but e.g. 7 frame
// GNSS position data takes about 3 ms. With unlapping offsets there should be only one message
// on buffer at time
//
// If you need to test new message for testing, write sender function - see. e.g. SendN2kTemperatureExt
// around line 360 (may change after update). Then add function to N2kSendMessages (somewhere line 504 )
// vector in similar way as SendN2kTemperatureExt. Set message period value according to message
// definition - most periods has been listed also on NMEA2000.cpp.
//
// Messages sending will be done by SendN2kMessages, which loops through message sender vector and sets
// loop NextSend scheduler to next nearest sent time. In this way function does not need every time
// loop through all messages. When it is time to send some messages, it loops them, sends necessary messages
// and sets NextSend to next nearest time. In ESP32 message sending could be own task and NextSend
// could be used just delay to leave time for other tasks.
//
// MessageSender has simple command line interface. With simple commands you e.g. disable all messages and
// then enable them one by one. To get available commands type ? and line feed to console to get help.

#include <Arduino.h>
#ifdef CORES3
#include <M5Unified.h>     // CoreS3: bring up board power rails (incl. M-Bus 5V for PwrCAN transceiver)
#include "driver/twai.h"   // CoreS3: read TWAI bus state for the status display + bus-off recovery
#endif
// #define N2k_SPI_CS_PIN 53    // If you use mcp_can and CS pin is not 53, uncomment this and modify definition to match your CS pin.
// #define N2k_CAN_INT_PIN 21   // If you use mcp_can and interrupt pin is not 21, uncomment this and modify definition to match your interrupt pin.
// #define USE_MCP_CAN_CLOCK_SET 8  // If you use mcp_can and your mcp_can shield has 8MHz chrystal, uncomment this.
// CAN pins default to the WROOM/node32s wiring; the m5stack-cores3 env overrides
// them to GPIO6/14 via -D build flags (hence the #ifndef guards).
#ifndef ESP32_CAN_TX_PIN
#define ESP32_CAN_TX_PIN GPIO_NUM_17  // 17 for transmit
#endif
#ifndef ESP32_CAN_RX_PIN
#define ESP32_CAN_RX_PIN GPIO_NUM_16  // 16 for receive
#endif
// #define NMEA2000_ARDUINO_DUE_CAN_BUS tNMEA2000_due::CANDevice1  // If you use Arduino DUE and want to use CAN bus 1 instead of 0, uncomment this.
// #define NMEA2000_TEENSY_CAN_BUS 1 // If you use Teensy 3.5 or 3.6 and want to use second CAN bus, uncomment this.
#include <N2kMessages.h>
#include <N2kTypes.h>
#include <NMEA2000_CAN.h>  // This will automatically choose right CAN library and create suitable NMEA2000 object

using tN2kSendFunction = void (*)();

// Structure for holding message sending information
struct tN2kSendMessage {
    tN2kSendFunction SendFunction;
    const char* const Description;
    tN2kSyncScheduler Scheduler;

    tN2kSendMessage(tN2kSendFunction _SendFunction, const char* const _Description, uint32_t /* _NextTime */,
                    uint32_t _Period, uint32_t _Offset, bool _Enabled)
        : SendFunction(_SendFunction), Description(_Description), Scheduler(_Enabled, _Period, _Offset) {}
    void Enable(bool state);
};

extern tN2kSendMessage N2kSendMessages[];
extern size_t nN2kSendMessages;

static unsigned long N2kMsgSentCount = 0;
static unsigned long N2kMsgFailCount = 0;
static bool ShowSentMessages = false;
static bool ShowStatistics = false;
static bool Sending = true;
static bool EnableForward = false;
static tN2kScheduler NextStatsTime;
static unsigned long StatisticsPeriod = 2000;

static unsigned char secondsSinceMidnight = 0;
static double windSpeed = 0.0;
static double windAngle = 0.0;

// Forward declarations for functions
void CheckCommand();
void CheckLoopTime();
void OnN2kOpen();

// *****************************************************************************
void setup() {
#ifdef CORES3
    auto cfg = M5.config();
    M5.begin(cfg);  // enables AXP2101 rails / M-Bus 5V so the PwrCAN transceiver is powered
#endif

    // Initialize serial port.
    Serial.begin(115200);
    delay(2000);  // Give a bit time for console to be ready

    // Reserve enough buffer for sending all messages.
    // This does not work on small memory devices like Uno or Mega
    // This version should work with smaller buffer, since message sending has been offsetted.
    NMEA2000.SetN2kCANSendFrameBufSize(250);
    // Set Product information
    NMEA2000.SetProductInformation("00000001",                // Manufacturer's Model serial code
                                   100,                       // Manufacturer's product code
                                   "Message sender example",  // Manufacturer's Model ID
                                   "1.1.2.35 (2022-10-01)",   // Manufacturer's Software version code
                                   "1.1.2.0 (2022-10-01)"     // Manufacturer's Model version
    );
    // Set device information
    NMEA2000.SetDeviceInformation(
        1,    // Unique number. Use e.g. Serial number.
        132,  // Device function=Analog to NMEA 2000 Gateway. See codes on https://web.archive.org/web/20190531120557/https://www.nmea.org/Assets/20120726%20nmea%202000%20class%20&%20function%20codes%20v%202.00.pdf
        25,  // Device class=Inter/Intranetwork Device. See codes on  https://web.archive.org/web/20190531120557/https://www.nmea.org/Assets/20120726%20nmea%202000%20class%20&%20function%20codes%20v%202.00.pdf
        2046  // Just choosen free from code list on https://web.archive.org/web/20190529161431/http://www.nmea.org/Assets/20121020%20nmea%202000%20registration%20list.pdf
    );
    // Uncomment 3 rows below to see, what device will send to bus
    // NMEA2000.SetForwardStream(&Serial);  // PC output to default serial port
    // NMEA2000.SetForwardType(tNMEA2000::fwdt_Text); // Show in clear text. Leave uncommented for default Actisense format.
    // NMEA2000.SetForwardOwnMessages(false); // Do not print own messages.

    // We act as real node on bus. Some devices does not show messages, if they can not request information.
    NMEA2000.SetMode(tNMEA2000::N2km_ListenAndNode, 22);
    // NMEA2000.SetDebugMode(tNMEA2000::dm_ClearText); // Uncomment this, so you can test code without CAN bus chips on Arduino Mega
    NMEA2000.EnableForward(EnableForward);  // Disable all msg forwarding to USB (=Serial)
    NMEA2000.SetOnOpen(OnN2kOpen);
    NMEA2000.Open();

    Serial.println("Starting message sender!");
    Serial.println("  Message sending to bus starts in 10 seconds.");
    Serial.println("  Type ?<lf> for available commands.");
}

// *****************************************************************************
void SendN2kMsg(const tN2kMsg& N2kMsg) {
    if (NMEA2000.SendMsg(N2kMsg)) {
        N2kMsgSentCount++;
    } else {
        N2kMsgFailCount++;
    }
    if (ShowSentMessages)
        N2kMsg.Print(&Serial);
}

// *****************************************************************************
// Function check is it time to send message. If it is, message will be sent and
// next send time will be updated.
// Function always returns next time it should be handled.
int64_t CheckSendMessage(tN2kSendMessage& N2kSendMessage) {
    if (N2kSendMessage.Scheduler.IsDisabled())
        return N2kSendMessage.Scheduler.GetNextTime();

    if (N2kSendMessage.Scheduler.IsTime()) {
        N2kSendMessage.Scheduler.UpdateNextTime();
        // This is the sender!!  Hard to find!
        N2kSendMessage.SendFunction();
    }

    return N2kSendMessage.Scheduler.GetNextTime();
}

// *****************************************************************************
// Function send enabled messages from tN2kSendMessage structure according to their
// period+offset.
void SendN2kMessages() {
    static uint64_t NextSend = 10;
    uint64_t Now = N2kMillis64();

    // Many of the message are sent periodically, and naively we might check them each every time step.
    // This code computes the next time a message might be triggered, and only checks them then (linear 
    // in the number of messages).  It then updates the next time to check.
    if (NextSend < Now) {
        // Learning, you can't print uint64's reliably (just too big??).
        // Serial.printf("SendN2KMessages time before: %6d %6d\n", int(Now), int(NextSend));
        uint64_t NextMsgSend;
        NextSend = Now + 2000;
        for (size_t i = 0; i < nN2kSendMessages; i++) {
            NextMsgSend = CheckSendMessage(N2kSendMessages[i]);
            if (NextMsgSend < NextSend) {
                NextSend = NextMsgSend;
            }
        }
        // Serial.printf("SendN2KMessages time after:  %6d %6d\n", int(Now), int(NextSend));
    }
}

// *****************************************************************************
#ifdef CORES3
// Status dashboard on the CoreS3 screen: bus state, TX ok/fail, error counters,
// enabled PGN count. Also self-heals the controller: an isolated-transceiver
// power glitch drives TWAI bus-off, which otherwise sticks until a reflash.
void updateSenderDisplay() {
    static unsigned long lastDisp = 0;
    if (millis() - lastDisp < 500) return;
    lastDisp = millis();

    twai_status_info_t st;
    bool ok = (twai_get_status_info(&st) == ESP_OK);
    const char* busState = "UNKNOWN";
    if (ok) {
        switch (st.state) {
            case TWAI_STATE_RUNNING:    busState = "RUNNING"; break;
            case TWAI_STATE_BUS_OFF:    busState = "BUS-OFF"; break;
            case TWAI_STATE_STOPPED:    busState = "STOPPED"; break;
            case TWAI_STATE_RECOVERING: busState = "RECOVER"; break;
        }
        if (st.state == TWAI_STATE_BUS_OFF)      twai_initiate_recovery();
        else if (st.state == TWAI_STATE_STOPPED) twai_start();
    }

    int enabled = 0;
    for (size_t i = 0; i < nN2kSendMessages; i++)
        if (N2kSendMessages[i].Scheduler.IsEnabled()) enabled++;

    M5.Display.fillScreen(TFT_BLACK);
    M5.Display.setCursor(0, 0);
    M5.Display.setTextSize(2);
    M5.Display.setTextColor(TFT_WHITE, TFT_BLACK);
    M5.Display.println("N2K SENDER");
    M5.Display.setTextColor((ok && st.state == TWAI_STATE_RUNNING) ? TFT_GREEN : TFT_RED, TFT_BLACK);
    M5.Display.printf("Bus: %s\n", busState);
    M5.Display.setTextColor(TFT_WHITE, TFT_BLACK);
    M5.Display.printf("TX ok:   %lu\n", N2kMsgSentCount);
    M5.Display.printf("TX fail: %lu\n", N2kMsgFailCount);
    if (ok)
        M5.Display.printf("TEC:%u REC:%u\n", (unsigned)st.tx_error_counter, (unsigned)st.rx_error_counter);
    M5.Display.printf("PGNs on: %d/%d\n", enabled, (int)nN2kSendMessages);
}
#endif

// *****************************************************************************
void loop() {
    if (Sending)
        SendN2kMessages();
    NMEA2000.ParseMessages();
    CheckCommand();
#ifdef CORES3
    updateSenderDisplay();
#endif

    if (ShowStatistics) {
        if (NextStatsTime.IsTime()) {
            NextStatsTime.FromNow(StatisticsPeriod);
            char buf[200];
            snprintf(buf, 200, "\nMessages sent:%lu, sent failed:%lu\n", N2kMsgSentCount, N2kMsgFailCount);
            Serial.print(buf);
        }

        CheckLoopTime();
    }
}

// *****************************************************************************
void SendN2kSystemTime() {
    tN2kMsg N2kMsg;
    // There were 19715 days since January 1, 1970, the Unix epoch.
    u_int16_t days = 19717;
    // seconds since midnight 9:10
    double secs = ((9 * 60) + 10) * 60 + millis() / 1000.0;
    
    SetN2kSystemTime(N2kMsg, 1, days, secs);
    SendN2kMsg(N2kMsg);
}
// *****************************************************************************
void SendN2kRudder() {
    tN2kMsg N2kMsg;
    SetN2kRudder(N2kMsg, DegToRad(5), 1, N2kRDO_MoveToStarboard, DegToRad(-5));
    SendN2kMsg(N2kMsg);
}
// *****************************************************************************
void SendN2kMagneticHeading() {
    static double Heading = 227.5;
    tN2kMsg N2kMsg;
    // Deviation is specific to the boat
    // Variation is specific to the location
    SetN2kMagneticHeading(N2kMsg, 0, DegToRad(Heading), DegToRad(-0.0), DegToRad(+15.5));
    SendN2kMsg(N2kMsg);
}
// *****************************************************************************
// rate of turn
void SendN2kRateOfTurn() {
    tN2kMsg N2kMsg;
    // Generate random number
    // int randomValue = random(0, 100);
    SetN2kRateOfTurn(N2kMsg, 0, 0.5);
    SendN2kMsg(N2kMsg);
}
// *****************************************************************************
void SendN2kAttitude() {
    tN2kMsg N2kMsg;
    SetN2kAttitude(N2kMsg, 1, DegToRad(-3.1), DegToRad(2.4), DegToRad(-7.8));
    SendN2kMsg(N2kMsg);
}
// *****************************************************************************
// magnetic variation
void SendN2kMagneticVariation() {
    tN2kMsg N2kMsg;
    // days since 1970-01-01
    int days = 17555;
    SetN2kMagneticVariation(N2kMsg, 0, tN2kMagneticVariation::N2kmagvar_Manual, days, DegToRad(15.5));
    SendN2kMsg(N2kMsg);
}

// *****************************************************************************
// Boat speed
void SendN2kBoatSpeed() {
    tN2kMsg N2kMsg;
    // Generate random number
    // int randomValue = random(0, 100);
    SetN2kBoatSpeed(N2kMsg, 0, 1.5);
    SendN2kMsg(N2kMsg);
}

// *****************************************************************************
// Water depth
void SendN2kWaterDepth() {
    tN2kMsg N2kMsg;
    // Generate random number
    // int randomValue = random(0, 100);
    SetN2kWaterDepth(N2kMsg, 0, 85, 1);
    SendN2kMsg(N2kMsg);
}
// *****************************************************************************
void SendN2kLatLonRapid() {
    tN2kMsg N2kMsg;
    // lat and lon of seattle
    SetN2kLatLonRapid(N2kMsg, 47.6062, -122.3321);
    SendN2kMsg(N2kMsg);
}
// *****************************************************************************
void SendN2kCOGSOGRapid() {
    tN2kMsg N2kMsg;
    SetN2kCOGSOGRapid(N2kMsg, 1, N2khr_true, DegToRad(115.6), 0.1);
    SendN2kMsg(N2kMsg);
}

// *****************************************************************************
void SendN2kGNSS() {
    tN2kMsg N2kMsg;
    // Days-since-1970 must be a realistic recent date (the receiver's clock-set gate accepts
    // only 2020..2030); 19717 ~= 2023-12-12, matching the SystemTime message. The original
    // example's 17555 (~2018) is rejected and would make GNSS useless as a time source.
    SetN2kGNSS(N2kMsg, 1, 19717, 62000, 60.436614990, 22.237819671, 10.5, N2kGNSSt_GPS, N2kGNSSm_GNSSfix, 12, 0.8, 0.5,
               15, 1, N2kGNSSt_GPS, 15, 2);
    SendN2kMsg(N2kMsg);
}
// *****************************************************************************
void SendN2kGNSSDOPData() {
    tN2kMsg N2kMsg;
    SetN2kGNSSDOPData(N2kMsg, 1, N2kGNSSdm_Auto, N2kGNSSdm_Auto, 1.2, -0.8, N2kDoubleNA);
    SendN2kMsg(N2kMsg);
}

// *****************************************************************************
void SendN2kWindSpeed() {
    tN2kMsg N2kMsg;
    // Generate random number
    // int randomValue = random(0, 100);
    SetN2kWindSpeed(N2kMsg, 0, 1.5, 30.0, tN2kWindReference::N2kWind_Apparent);
    SendN2kMsg(N2kMsg);
}

// *****************************************************************************
void SendN2kProductInformation() {
    NMEA2000.SendProductInformation();
    N2kMsgSentCount++;
}

// *****************************************************************************
void SendN2kIsoAddressClaim() {
    // Note that sometime NMEA Reader gets grazy, when ISO Address claim will be sent periodically.
    // If that happens, reopen NMEA Reader.
    NMEA2000.SendIsoAddressClaim();
    N2kMsgSentCount++;
}

// *****************************************************************************
void SendN2kAISClassAPosition() {
    tN2kMsg N2kMsg;
                            N2kaischannel_A_VDL_reception, 30, 0, tN2kAISNavStatus::N2kaisns_At_Anchor);
    SendN2kMsg(N2kMsg);
}

// We add 300 ms as default for each offset to avoid failed sending at start.
// Message sending is synchronized to open. After open there is 250 ms address claiming time when
// message sending fails.
#define AddSendPGN(fn, NextSend, Period, Offset, Enabled) \
    { fn, #fn, NextSend, Period, Offset + 300, Enabled }

tN2kSendMessage N2kSendMessages[] = {
    AddSendPGN(SendN2kIsoAddressClaim, 0, 5000, 0, false)  // 60928 Not periodic
    ,
    AddSendPGN(SendN2kProductInformation, 0, 5000, 60, false)  // 126996 (20) Not periodic
    ,
    AddSendPGN(SendN2kSystemTime, 0, 1000, 0, true)  // 126992  (enabled by default: sets receiver clock -> timestamped log name)
    ,
    AddSendPGN(SendN2kRudder, 0, 100, 0, true)  // 127245
    ,
    AddSendPGN(SendN2kMagneticHeading, 0, 100, 1, true)  // 127250
    ,
    AddSendPGN(SendN2kRateOfTurn, 0, 100, 3, true)  // 127251
    ,
    AddSendPGN(SendN2kAttitude, 0, 100, 0, false)  // 127257
    ,
    AddSendPGN(SendN2kMagneticVariation, 0, 100, 2, false)  // 127258
    ,
    AddSendPGN(SendN2kBoatSpeed, 0, 100, 0, true)  // 128259
    ,
    AddSendPGN(SendN2kWaterDepth, 0, 100, 0, true)  // 128267
    ,
    AddSendPGN(SendN2kLatLonRapid, 0, 100, 0, true)  // 129025
    ,
    AddSendPGN(SendN2kCOGSOGRapid, 0, 100, 0, true)  // 129026
    ,
    AddSendPGN(SendN2kGNSS, 0, 1000, 75, true)  // 129029 (7)
    ,
    AddSendPGN(SendN2kGNSSDOPData, 0, 1000, 37, false)  // 129539
    ,
    AddSendPGN(SendN2kWindSpeed, 0, 100, 0, true)  // 130306
};

size_t nN2kSendMessages = sizeof(N2kSendMessages) / sizeof(tN2kSendMessage);

// *****************************************************************************
// Call back for NMEA2000 open. This will be called, when library starts bus communication.
// See NMEA2000.SetOnOpen(OnN2kOpen); on setup()
// We initialize here all messages next sending time. Since we use tN2kSyncScheduler all messages
// send offset will be synchronized to libary.
void OnN2kOpen() {
    Serial.println("NMEA2000 Opened!");
    for (size_t i = 0; i < nN2kSendMessages; i++) {
        if (N2kSendMessages[i].Scheduler.IsEnabled())
            N2kSendMessages[i].Scheduler.UpdateNextTime();
    }
    // Sending=true;
}

// *****************************************************************************
// Command handling functions. These are environment functions for example not
// for how to use library.

// *****************************************************************************
void CheckLoopTime() {
#define LoopTimePeriod 1000
    static unsigned long NextCheck = millis() + LoopTimePeriod;
    static unsigned long AvgCount = 0;
    static float AvgSum = 0;
    static unsigned long MaxLoopTime = 0;
    static unsigned long StartTime = micros();

    unsigned long UsedTime = micros() - StartTime;
    if (UsedTime > MaxLoopTime)
        MaxLoopTime = UsedTime;
    AvgCount++;
    AvgSum += UsedTime;

    if (NextCheck < millis()) {
        NextCheck = millis() + LoopTimePeriod;
        Serial.print("- Loop times max:");
        Serial.print(MaxLoopTime);
        Serial.print(" us, avg:");
        Serial.print(AvgSum / AvgCount);
        Serial.println(" us");
        MaxLoopTime = 0;
        AvgSum = 0;
        AvgCount = 0;
    }

    StartTime = micros();
}

// *****************************************************************************
void tN2kSendMessage::Enable(bool state) {
    if (Scheduler.IsEnabled() != state) {
        if (state) {
            Scheduler.UpdateNextTime();
        } else {
            Scheduler.Disable();
        }
    }
}

using tCommandFunction = void (*)(const char* Params);

struct tCommand {
    const char* const Command;
    const char* const Usage;
    const char* const Description;
    tCommandFunction CommandFunction;
};

// *****************************************************************************
void ToggleStats(const char*) {
    ShowStatistics = !ShowStatistics;
    if (ShowStatistics) {
        NextStatsTime.FromNow(StatisticsPeriod);
    } else {
        NextStatsTime.Disable();
    }
}

// *****************************************************************************
void ToggleSending(const char*) {
    Sending = !Sending;
    Serial.print("Message sending ");
    Serial.println(Sending ? "enabled" : "disabled");
}

// *****************************************************************************
void ToggleShowSentMessages(const char*) {
    ShowSentMessages = !ShowSentMessages;
}

// *****************************************************************************
void ToggleForward(const char*) {
    EnableForward = !EnableForward;
    NMEA2000.EnableForward(EnableForward);

    Serial.print("Show bus messages ");
    Serial.println(EnableForward ? "enabled" : "disabled");
}

// *****************************************************************************
void ClearStatistics(const char*) {
    N2kMsgSentCount = 0;
    N2kMsgFailCount = 0;
    Serial.println("Message statistics cleared");
}

// *****************************************************************************
bool HasParam(const char*& Params) {
    if (Params == 0)
        return false;

    while (*Params == ' ' || *Params == '\t')
        Params++;  // Pass spaces to check param exist
    if (*Params == 0) {
        return false;  // Param not defined.
    }

    return true;
}

// *****************************************************************************
template <typename T>
bool GetParam(const char*& Params, T& Value, T Def = 0) {
    Value = Def;
    char* ParamEnd;
    Value = strtol(Params, &ParamEnd, 10);
    bool ret = (Params != ParamEnd && (*ParamEnd == 0 || *ParamEnd == ' ' || *ParamEnd == '\t'));
    Params = ParamEnd;
    return ret;
}

// *****************************************************************************
void ToggleMsgSending(const char* Params) {
    if (!HasParam(Params)) {
        Serial.println("Message index not defined");
        return;
    }

    size_t MsgIndex;
    if (!GetParam(Params, MsgIndex) || MsgIndex >= nN2kSendMessages) {
        Serial.println("Invalid message index");
        return;
    }

    N2kSendMessages[MsgIndex].Enable(!N2kSendMessages[MsgIndex].Scheduler.IsEnabled());
    Serial.print(N2kSendMessages[MsgIndex].Description);
    Serial.print("  ");
    Serial.println(N2kSendMessages[MsgIndex].Scheduler.IsEnabled() ? "enabled" : "disabled");
}

// *****************************************************************************
void SendMessageNow(const char* Params) {
    if (!HasParam(Params)) {
        Serial.println("Message index not defined");
        return;
    }

    size_t MsgIndex;
    if (!GetParam(Params, MsgIndex) || MsgIndex >= nN2kSendMessages) {
        Serial.println("Invalid message index");
        return;
    }

    N2kSendMessages[MsgIndex].SendFunction();
    Serial.print(N2kSendMessages[MsgIndex].Description);
    Serial.println("  sent");
}

// *****************************************************************************
void SetMessagePeriod(const char* Params) {
    if (!HasParam(Params)) {
        Serial.println("Message index not defined");
        return;
    }

    size_t MsgIndex;
    if (!GetParam(Params, MsgIndex) || MsgIndex >= nN2kSendMessages) {
        Serial.println("Invalid message index");
        return;
    }

    if (HasParam(Params)) {
        uint32_t Period;
        if (!GetParam(Params, Period) || Period > 60000) {
            Serial.println("Invalid period");
            return;
        }

        N2kSendMessages[MsgIndex].Scheduler.SetPeriod(Period);
    }
    Serial.print(N2kSendMessages[MsgIndex].Description);
    Serial.print("  period is ");
    Serial.println(N2kSendMessages[MsgIndex].Scheduler.GetPeriod());
}

// *****************************************************************************
void SetMessageOffset(const char* Params) {
    if (!HasParam(Params)) {
        Serial.println("Message index not defined");
        return;
    }

    size_t MsgIndex;
    if (!GetParam(Params, MsgIndex) || MsgIndex >= nN2kSendMessages) {
        Serial.println("Invalid message index");
        return;
    }

    if (HasParam(Params)) {
        uint32_t Offset;
        if (!GetParam(Params, Offset) || Offset > 60000) {
            Serial.println("Invalid offset");
            return;
        }

        N2kSendMessages[MsgIndex].Scheduler.SetOffset(Offset);
    }
    Serial.print(N2kSendMessages[MsgIndex].Description);
    Serial.print("  offset is ");
    Serial.println(N2kSendMessages[MsgIndex].Scheduler.GetOffset());
}

// *****************************************************************************
void EnableAllMessages(const char*) {
    for (size_t i = 0; i < nN2kSendMessages; i++) {
        N2kSendMessages[i].Enable(true);
    }
    Serial.println("All messages enabled");
}

// *****************************************************************************
void DisableAllMessages(const char*) {
    for (size_t i = 0; i < nN2kSendMessages; i++) {
        N2kSendMessages[i].Enable(false);
    }
    Serial.println("All messages disabled");
}

// *****************************************************************************
void ListMessages(const char*) {
    for (size_t i = 0; i < nN2kSendMessages; i++) {
        Serial.print("    ");
        Serial.print(i);
        Serial.print(" : ");
        Serial.print(N2kSendMessages[i].Scheduler.IsEnabled() ? "(enabled)  " : "(disabled) ");
        Serial.println(N2kSendMessages[i].Description);
    }
}

void ListCommands(const char* Command);

tCommand Commands[] = {{"?", 0, "List commands", ListCommands},
                       {"p", 0, "Toggle sending", ToggleSending},
                       {"stat", 0, "Toggle stat printing", ToggleStats},
                       {"cl", 0, "Clear statistics", ClearStatistics},
                       {"e", 0, "Enable all messages", EnableAllMessages},
                       {"d", 0, "Disable all messages", DisableAllMessages},
                       {"s", 0, "Toggle show sent messages", ToggleShowSentMessages},
                       {"fwd", 0, "Toggle show other bus messages", ToggleForward},
                       {"msgs", 0, "List messages", ListMessages},
                       {"msg", "msg <x>", "Toggle message x sending", ToggleMsgSending},
                       {"once", "once <x>", "Sends message x on request", SendMessageNow},
                       {"period", "period <x> [p]",
                        "Set message x period to p. If p is not defined, just shows period.", SetMessagePeriod},
                       {"offset", "offset <x> [o]",
                        "Set message x offset to o. If o is not defined, just shows offset.", SetMessageOffset}};

size_t nCommands = sizeof(Commands) / sizeof(tCommand);

// *****************************************************************************
void ListCommands(const char* Command) {
    Serial.println();
    Serial.println("Commands:");
    char Info[200];
    for (size_t i = 0; i < nCommands; i++) {
        strcpy(Info, "  ");
        strcat(Info, Commands[i].Usage != 0 ? Commands[i].Usage : Commands[i].Command);
        size_t len = strlen(Info);
        if (len < 16) {
            memset(Info + len, ' ', 16 - len);
            Info[16] = 0;
        }
        strcat(Info, "- ");
        strcat(Info, Commands[i].Description);
        Serial.println(Info);
    }
    Serial.println();
}

// *****************************************************************************
void CheckCommand() {
    static char Command[20] = {0};
    static size_t CommandPos = 0;

    if (Serial.available()) {
        char ch = Serial.read();
        if (ch == '\n') {
            char* Params = strchr(Command, ' ');
            bool FoundCommand = false;
            if (Params != 0) {

                *Params = 0;
                Params++;
            }
            for (size_t i = 0; i < nCommands; i++) {
                if (strcmp(Command, Commands[i].Command) == 0) {
                    Commands[i].CommandFunction(Params);
                    FoundCommand = true;
                    break;
                }
            }
            if (!FoundCommand) {
                Serial.print("Unknown command ");
                Serial.println(Command);
            }
            CommandPos = 0;
            Command[CommandPos] = 0;
        } else {
            if (ch >= ' ' && CommandPos + 1 < sizeof(Command)) {
                Command[CommandPos] = ch;
                CommandPos++;
                Command[CommandPos] = 0;
            }
        }
    }
}