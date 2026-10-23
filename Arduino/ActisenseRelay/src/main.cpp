// See README.md for more information

#include <Arduino.h>
#define DEBUGLOG_DEFAULT_LOG_LEVEL_INFO
// Include DebugLog after that
#include <DebugLog.h>

#define ESP32_CAN_TX_PIN GPIO_NUM_17  // 17 for tranmit
#define ESP32_CAN_RX_PIN GPIO_NUM_16  // 16 for receive

#include <ActisenseReader.h>
#include <N2kMsg.h>
#include <NMEA2000.h>
#include <NMEA2000_CAN.h>
#include <queue>
#include <vector>
#include "CharArrayStream.hpp"
#include "nmea.hpp"
#include "utils.hpp"

using namespace utils;

tActisenseReader ActisenseReader;

void HandleStreamN2kMsg(const tN2kMsg& N2kMsg) {
    // N2kMsg.Print(&Serial);
    NMEA2000.SendMsg(N2kMsg, -1);
}

// Consumes the text version of the N2k message captured from Actisense (from the boat) and computes an N2k message.
static bool actisenseToN2k(const char* cmd, tN2kMsg& N2kMsg) {
    unsigned int prio;
    unsigned int pgn;
    unsigned int src;
    unsigned int dst;
    unsigned int bytes;

    char* p;
    int i;
    int b;
    unsigned int byt;
    int r;

    if (!cmd || !*cmd || *cmd == '\n') {
        return false;
    }

    // Skip the time field, content is not used
    p = strchr((char*)cmd, ',');
    if (!p) {
        return false;
    }

    r = sscanf(p, ",%u,%u,%u,%u,%u,%n", &prio, &pgn, &src, &dst, &bytes, &i);
    // print the results
    LOG_DEBUG("prio: %u, pgn: %u, src: %u, dst: %u, bytes: %u, i: %u\n", prio, pgn, src, dst, bytes, i);
    if (r == 5) {
        N2kMsg.SetPGN(pgn);
        N2kMsg.Priority = prio;
        // These messages were captured in the past, we are going to try to replay.  Let's see if it works.
        N2kMsg.Source = src;
        N2kMsg.Destination = dst;
        p += i - 1;
        for (b = 0; b < bytes; b++) {
            if ((sscanf(p, ",%x%n", &byt, &i) == 1) && (byt < 256)) {
                N2kMsg.AddByte(byt);
            } else {
                LOG_WARN("ActisenseToN2k error pgn: %u '%s'\n", pgn, cmd);
                LOG_WARN("   offset %d of %d\n", b, bytes);
                return false;
            }
            p += i;
        }
    } else {
        LOG_WARN("ActisenseToN2k sscanf '%s'\n", cmd);
        LOG_WARN("  sscanf '%s', r = %d\n", cmd, r);
        LOG_WARN("  prio: %u, pgn: %u, src: %u, dst: %u, bytes: %u, i: %u\n", prio, pgn, src, dst, bytes, i);
        return false;
    }
    return true;
}

void OnN2kOpen() {
    Serial.println("NMEA2000 Opened!");
}

void setupN2k() {
    // Define buffers big enough
    NMEA2000.SetN2kCANSendFrameBufSize(250);
    NMEA2000.SetN2kCANReceiveFrameBufSize(150);

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

    // We act as real node on bus. Some devices does not show messages, if they can not request information.
    NMEA2000.SetMode(tNMEA2000::N2km_ListenAndNode, 100);
    // NMEA2000.SetDebugMode(tNMEA2000::dm_ClearText); // Uncomment this, so you can test code without CAN bus chips on Arduino Mega
    NMEA2000.EnableForward(false);  // Disable all msg forwarding to USB (=Serial)
    NMEA2000.SetOnOpen(OnN2kOpen);
    NMEA2000.Open();
}

int countParse = 0;
int N2kMsgSentCount = 0;
int N2kMsgFailCount = 0;
int lineQueueHighWaterMark = 0;
int parsingTime = 0;
int messages = 0;
int readTime = 0;

// *****************************************************************************
void SendN2kMsg(const tN2kMsg& N2kMsg) {
    // -1 is magic to resend this message using the "sender" encoded in the message
    if (NMEA2000.SendMsg(N2kMsg, -1)) {
        N2kMsgSentCount++;
    } else {
        N2kMsgFailCount++;
    }
}

// Given an Actisense message, parse it and send it to the N2k bus
void lineParse(const char* textMessage) {
    countParse++;
    int start = millis();
    LOG_DEBUG("Testing parse of message:\n", textMessage);

    tN2kMsg N2kMsg;
    bool success = actisenseToN2k(textMessage, N2kMsg);
    if (!success) {
        LOG_ERROR("Failed to parse message: %s", textMessage);
        return;
    } else {
        // show PGN we parsed
        LOG_DEBUG("Parsed PGN: %d", N2kMsg.PGN);
        // Send out the bus
        SendN2kMsg(N2kMsg);
    }
    // Handle it locally as well, as if we had sent it.
    nmea::HandleNMEA2000Msg(N2kMsg);
    int end = millis();
    parsingTime += end - start;
}

void testMessages() {
    // create string constants
    // start time in millis()
    unsigned long start = millis();

    lineParse("2023-10-29T18:53:08.553Z,2,127245,15,255,8,ff,ff,ff,7f,28,05,ff,ff");
    lineParse("2023-10-29T18:53:08.553Z,2,127251,4,255,8,ff,14,aa,05,00,ff,ff,ff");
    lineParse("2023-10-29T18:53:08.553Z,3,65323,16,255,8,41,9f,ff,68,64,ff,ff,ff");
    lineParse("2023-10-29T18:53:08.553Z,2,129025,5,255,8,4e,7f,6c,1c,f6,74,08,b7");
    lineParse("2023-10-29T18:53:08.553Z,3,130840,3,255,17,41,9f,ff,68,64,01,ff,ff,b9,8b,76,22,00,8c,50,c0,ff");
    lineParse("2023-10-29T18:53:08.553Z,6,128275,3,255,14,ff,ff,ff,ff,ff,ff,c8,a3,4b,00,c8,a3,4b,00");
    lineParse("2023-10-29T18:53:08.553Z,3,129799,8,255,19,a3,fd,ee,00,a3,fd,ee,00,37,32,00,00,00,00,19,00,00,00,00");
    lineParse("2023-10-29T18:53:08.553Z,2,127250,4,255,8,ff,eb,0b,ff,7f,ff,7f,fd");
    lineParse("2023-10-29T18:53:08.553Z,3,130840,1,255,17,41,9f,ff,68,64,01,ff,ff,b9,8b,76,22,00,8c,50,c0,ff");
    lineParse("2023-10-29T18:53:08.553Z,2,127245,15,255,8,ff,ff,ff,7f,30,05,ff,ff");
    lineParse("2023-10-29T18:53:08.599Z,2,129026,127,255,8,00,fc,98,15,35,01,ff,ff");
    lineParse("2023-10-29T18:53:08.599Z,2,129025,127,255,8,26,7e,6c,1c,47,74,08,b7");
    lineParse("2023-10-29T18:53:08.599Z,2,127250,128,255,8,00,4b,05,ff,7f,e7,09,fd");
    lineParse("2023-10-29T18:53:08.600Z,2,127251,4,255,8,ff,21,a0,ff,ff,ff,ff,ff");
    lineParse("2023-10-29T18:53:08.600Z,3,127251,128,255,8,00,13,7b,04,00,51,7e,28");
    lineParse("2023-10-29T18:53:08.600Z,2,130306,3,255,8,ff,2c,02,10,d8,fb,ff,ff");
    lineParse("2023-10-29T18:53:08.600Z,3,129799,8,255,19,a3,fd,ee,00,a3,fd,ee,00,37,32,00,00,00,00,19,00,00,00,00");
    lineParse("2023-10-29T18:53:08.600Z,7,127258,11,255,8,00,ff,ff,ff,e7,09,ff,ff");
    lineParse("2023-10-29T18:53:08.600Z,2,127250,4,255,8,ff,f1,0b,ff,7f,ff,7f,fd");
    lineParse("2023-10-29T18:53:08.600Z,2,130306,14,255,8,00,a3,03,b4,e8,fa,ff,ff");
    lineParse("2023-10-29T18:53:08.600Z,2,127245,15,255,8,ff,ff,ff,7f,3d,05,ff,ff");
    lineParse("2023-10-29T18:53:08.621Z,7,127258,12,255,8,16,ff,ff,ff,e7,09,ff,ff");
    lineParse("2023-10-29T18:53:08.621Z,2,127251,4,255,8,ff,82,8d,fb,ff,ff,ff,ff");
    lineParse("2023-10-29T18:53:08.621Z,2,129025,5,255,8,66,7f,6c,1c,0f,75,08,b7");
    lineParse("2023-10-29T18:53:08.652Z,2,127250,4,255,8,ff,f1,0b,ff,7f,ff,7f,fd");
    lineParse("2023-10-29T18:53:08.653Z,3,129799,8,255,19,a3,fd,ee,00,a3,fd,ee,00,37,32,00,00,00,00,19,00,00,00,00");
    lineParse("2023-10-29T18:53:08.653Z,2,127245,15,255,8,ff,ff,ff,7f,45,05,ff,ff");
    lineParse("2023-10-29T18:53:08.724Z,2,129026,127,255,8,00,fc,d4,15,37,01,ff,ff");
    lineParse("2023-10-29T18:53:08.724Z,2,129025,127,255,8,3d,7e,6c,1c,60,74,08,b7");
    lineParse("2023-10-29T18:53:08.724Z,2,127250,128,255,8,00,23,05,ff,7f,e7,09,fd");
    lineParse("2023-10-29T18:53:08.724Z,2,127251,4,255,8,ff,39,7f,f6,ff,ff,ff,ff");
    lineParse("2023-10-29T18:53:08.724Z,3,127251,128,255,8,00,96,57,03,00,74,08,b7");
    lineParse("2023-10-29T18:53:08.724Z,7,127258,10,255,8,00,ff,ff,ff,e7,09,ff,ff");
    lineParse("2023-10-29T18:53:08.724Z,2,127245,3,255,8,ff,ff,ff,7f,ff,7f,ff,ff");
    lineParse("2023-10-29T18:53:08.724Z,7,127258,13,255,8,16,ff,ff,ff,e7,09,ff,ff");
    lineParse("2023-10-29T18:53:08.724Z,2,127250,4,255,8,ff,ef,0b,ff,7f,ff,7f,fd");
    lineParse("2023-10-29T18:53:08.724Z,2,130306,14,255,8,00,a3,03,c7,e7,fa,ff,ff");
    lineParse("2023-10-29T18:53:09.400Z,2,128259,35,255,8,ce,36,01,ff,ff,00,ff,ff");
    lineParse("2023-10-29T18:53:09.400Z,3,128267,35,255,8,be,ac,1c,00,00,00,00,ff");

    // Total time and messages per second
    unsigned long end = millis();
    unsigned long duration = end - start;
    // log counts, time, and msg/sec
    LOG_INFO("Parsed %d messages in %dms, average: %f msg/sec\n", countParse, duration, countParse / (duration / 1000.0));
    nmea::printState(&Serial);
}

std::queue<String> lines;
char buffer[512];

// freertos task to print the state, and other stats
void printState(void* parameter) {
    while (true) {
        Serial.printf("Free heap: %d, high water mark: %d\n", xPortGetFreeHeapSize(),
                      uxTaskGetStackHighWaterMark(NULL));
        Serial.printf("String queue size %d\n", int(lines.size()));
        Serial.printf("Parsing time: %dms, messages: %d, average: %f msg/sec\n", parsingTime, countParse,
                      countParse / (parsingTime / 1000.0));
        // reading time
        Serial.printf("Reading time: %dms, average: %f msg/sec\n", readTime, countParse / (readTime / 1000.0));
        nmea::printState(&Serial);
        vTaskDelay(3000 / portTICK_PERIOD_MS);
    }
}

// task that simply reads lines and collects them
void readLines(void* parameter) {
    int len = 0;
    int start, end;
    while (true) {
        start = end = millis();
        // read a line from the serial port
        utils::readCommand(buffer, sizeof(buffer), len);
        if (len > 0) {
            end = millis();
            LOG_DEBUG("ReadLines: line count %4d line: %s\n", int(lines.size()), buffer);
            lines.push(String(buffer));
            if (lines.size() > 50) {
                LOG_WARN("ReadLines: line count", int(lines.size()));
            }
        } else {
            // yeild the task
            vTaskDelay(1 / portTICK_PERIOD_MS);
        }
        readTime += end - start;
    }
}

#ifdef ESP32_S3_DEVKITC_1
int receiveLoopCount = 0;
void receiveNmeaMessage(void *parameter) {
    LOG_INFO("receiveNmeaMessage: startup\n");

    while (true) {
        receiveLoopCount++;
        ESP32_CAN_read_frame();
        if (receiveLoopCount % 100 == 0) {
            LOG_INFO("receiveNmeaMessage: %d stack: %6d\n", receiveLoopCount, uxTaskGetStackHighWaterMark(NULL));
        }
        vTaskDelay(5 / portTICK_PERIOD_MS);
    }
}
#endif

void setup() {
    delay(1000);
    Serial.setRxBufferSize(1024);
    Serial.begin(115200, SERIAL_8N1);
    Serial.end();
    Serial.begin(115200, SERIAL_8N1);    
    //   Serial.begin(115200);
    while (!Serial) {
        ;  // wait for serial port to connect. Needed for native USB port only
    }
    Serial.println("Starting NMEA2000 Actisense Relay");
    setupN2k();
    // create the threads
    xTaskCreatePinnedToCore(printState, "printState", 10000, NULL, 2, NULL, 0);
    xTaskCreatePinnedToCore(readLines, "readLines", 32 * 1024, NULL, 1, NULL, 0);

#ifdef ESP32_S3_DEVKITC_1
    xTaskCreatePinnedToCore(receiveNmeaMessage,    /* Function to implement the task */
                            "Task3",               /* Name of the task */
                            4096,                  /* Stack size in words */
                            NULL,                  /* Task input parameter */
                            2 | portPRIVILEGE_BIT, /* Priority of the task */
                            NULL,                  /* Task handle. */
                            0);                    /* Core where the task should run */
#endif       
}

void loop() {
    int len = 0;
    if (lines.size() > 0) {
        lineQueueHighWaterMark = lineQueueHighWaterMark < lines.size() ? lines.size() : lineQueueHighWaterMark;
        String line = lines.front();
        lines.pop();
        LOG_DEBUG("Loop: line count line: %s\n", int(lines.size()), line.c_str());
        lineParse(line.c_str());
    }
}
