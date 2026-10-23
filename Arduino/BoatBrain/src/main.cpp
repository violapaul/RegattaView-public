#include "board.hpp"
#include "display.hpp"

#include <ctime>
#include "WiFiClient.h"
#include "WiFiUdp.h"
#include <FS.h>

#include "sdcard.hpp"
#include "nmea.hpp"
// #include "sdunix.hpp"
#include "utils.hpp"
#include "webServer.hpp"
#include "ledS3.hpp"
#include "power.hpp"  // optional M5GO Bottom3 battery + boat-power failover (CORES3)


#define LOG_ROTATE_MINS 20 // rotate the log file every N minutes
// Set these to your desired credentials.
#include "wifi_credentials.hpp"

#define WIFI_MYAP "BoatBrain"


#define UDP_PORT 101
#define TCP_PORT 100
#define MAX_UDP_CLIENTS 4
#define UDP_CLIENT_TIMEOUT_MS 10000  // Remove client if no hello for 10s


int N2K_MSG_COUNT = 0;
double N2K_MSG_RATE = 0.0;
int N2K_PREV_MSG_COUNT = 0;
uint32_t batteryMs = 0;  // ms continuously on reserve battery (0 on boat power)
double lastLogTime = -1.0;        // Last time we rotated the log file
bool defaultLogging = true;   
String loggingFile;
File loggingStream;

LED* led1;
LED* led2;

// Display, use this if you are not printing/logging to the LCD screen.
void displaySetup();
void displayString(const String& str, int x = 10, int y = 10, bool clear = true);
void displayBoatState(const nmea::tBoatState& state);

// Swap the NMEA data log file to a new file.  If the file exists, close it.  If it doesn't exist, create it.
void swapDataLogFile(String loggingFile) {
    if (!sdcard::hasCard()) {
        return;
    }
    LOGF_INFO("Swapping log file to %s\n", loggingFile.c_str());
    // if it exists, close last loggingStream
    if (loggingStream) {
        // disable NMEA logging
        nmea::LoggingStream = NULL;
        LOG_INFO("Closing last log file.");
        loggingStream.close();
    }
    // open file
    loggingStream = sdcard::openFile(loggingFile.c_str(), FILE_WRITE);
    if (!loggingStream) {
        LOG_ERROR("Failed to open file for writing");
        return;
    }
    LOG_DEBUG("File opened for writing.");
    // enable logging
    nmea::LoggingStream = &loggingStream;
}

// There are two types of data logs.  One is the default log file, which is created when the GPS
// time is not set.  This is by convention a six digit number, like 000003.log.  The other is the
// real time log file, which is created when the GPS time is set.  This is by convention a date time.


// find all the existing "default log files" and return the next one in sequence.
String uniqueDefaultLogFileName(String extension) {
    auto files = sdcard::listFiles("/", "");
    LOGF_DEBUG("Found %d files\n", files.size());
    // loop over files and check if they match
    String lastLog = "none.log";
    int maxLog = 0;
    for (auto fileSize : files) {
        auto file = fileSize.filename;
        if (!file.endsWith(extension)) {
            continue;
        }
        // starts with 0
        if (file.startsWith("0")) {
            LOGF_DEBUG("Found log file: %s\n", file.c_str());
            // get the number
            int num = file.substring(0, 6).toInt();
            if (num > maxLog) {
                maxLog = num;
                lastLog = file;
            }
        }
    }
    LOGF_DEBUG("Last log file: %s\n", lastLog.c_str());

    // create a zero padded 6 digit number using sprintf
    char* newFile = new char[20];
    sprintf(newFile, "/%06d", maxLog+1);
    return String(newFile);
}

// convert a unixtime to a filename, e.g. 2021-01-01T00:00:00Z
String unixtimeToFilename(time_t unixtime) {
    struct tm* timeinfo;
    char buffer[40];
    timeinfo = std::gmtime(&unixtime);
    std::strftime(buffer, sizeof(buffer), "%Y-%m-%dT%H_%M_%SZ", timeinfo);
    return String(buffer);
}


// Rotate the data log file every N minutes
void rotateDataLogFile() {
    LOG_INFO("Rotating log file.");
    // convert to unixtime to filename, e.g. 2021-01-01T00:00:00Z.txt
    loggingFile = "/" + unixtimeToFilename(nmea::boatState.getTime()) + ".log";
    LOGF_INFO("New DLog file: %s", loggingFile.c_str());
    swapDataLogFile(loggingFile);
}

// Maintain the logs.  If the GPS time is not set, log to a default log file.  If the GPS time is set,
// rotate the log file every N minutes.
void maintainLogs() {
    if (!sdcard::hasCard()) {
        return;
    }
    // Logic, wait for the GPT time to be set.  Then create a new log file.  And enable logging.
    // After that wait, N mins and create a new log file.
    if (nmea::boatState.getTime() > 0) {
        if (defaultLogging) {
            defaultLogging = false;
            rotateDataLogFile();
            lastLogTime = nmea::boatState.getTime();
        } else if (nmea::boatState.getTime() - lastLogTime > 60 * LOG_ROTATE_MINS) {
            rotateDataLogFile();
            lastLogTime = nmea::boatState.getTime();
        }
    } else {
        // If the GPS time is not yet set, log data to a default log file.
        double cpuSeconds = millis() / 1000.0;
        if (lastLogTime < 0.0 || (cpuSeconds - lastLogTime > 60 * LOG_ROTATE_MINS)) {
            String defaultLogFile = uniqueDefaultLogFileName(".log") + ".log";
            swapDataLogFile(defaultLogFile);
            lastLogTime = cpuSeconds;
        }
    }
}

void nmeaDataLoggerLoopTask(void* pvParameters) {
    LOG_INFO("nmeaDataLoggerLoopTask started");
    while(1) {
        maintainLogs();
        nmea::loopStep();
        vTaskDelay(2 / portTICK_PERIOD_MS);
    }
}


// a class which is used to compute exponential moving average, of times between events
class RunningAverage {
   public:
    RunningAverage(float alpha) {
        this->alpha = alpha;
        this->average = 0;
        this->lastEventTime = millis();
    }

    float event() {
        unsigned long now = millis();
        float delta = now - lastEventTime;
        lastEventTime = now;
        average = (average * alpha) + (delta * (1 - alpha));
        return average;
    }

    float getAverage() { return average; }

   private:
    float alpha;
    float average;
    unsigned long lastEventTime;
};

// UDP client tracking
struct UdpClient {
    IPAddress ip;
    uint16_t port;
    unsigned long lastHello;
    bool active;
};

UdpClient udpClients[MAX_UDP_CLIENTS];
int udpClientCount = 0;

void udpAddClient(IPAddress ip, uint16_t port) {
    unsigned long now = millis();
    // Check if client already exists
    for (int i = 0; i < MAX_UDP_CLIENTS; i++) {
        if (udpClients[i].active && udpClients[i].ip == ip) {
            udpClients[i].lastHello = now;
            udpClients[i].port = port;
            return;
        }
    }
    // Add new client
    for (int i = 0; i < MAX_UDP_CLIENTS; i++) {
        if (!udpClients[i].active) {
            udpClients[i].ip = ip;
            udpClients[i].port = port;
            udpClients[i].lastHello = now;
            udpClients[i].active = true;
            udpClientCount++;
            LOGF_INFO("UDP client added: %s:%d (total: %d)", ip.toString().c_str(), port, udpClientCount);
            return;
        }
    }
    LOG_WARN("UDP client slots full");
}

void udpCleanupClients() {
    unsigned long now = millis();
    for (int i = 0; i < MAX_UDP_CLIENTS; i++) {
        if (udpClients[i].active && (now - udpClients[i].lastHello) > UDP_CLIENT_TIMEOUT_MS) {
            LOGF_INFO("UDP client timeout: %s", udpClients[i].ip.toString().c_str());
            udpClients[i].active = false;
            udpClientCount--;
        }
    }
}

// UDP task: listens for hello packets, sends boat state to all known clients
void boatStateUdpTask(void* pvParameters) {
    LOG_INFO("boatStateUdpTask started");
    WiFiUDP udp;

    // Initialize client list
    for (int i = 0; i < MAX_UDP_CLIENTS; i++) {
        udpClients[i].active = false;
    }

    // Start listening
    if (!udp.begin(UDP_PORT)) {
        LOG_ERROR("Failed to start UDP listener");
        vTaskDelete(NULL);
        return;
    }
    LOGF_INFO("UDP listening on port %d", UDP_PORT);

    unsigned char msgBuffer[128];
    unsigned char terminator[3] = {0xff, 0xff, '\n'};
    TickType_t lastRunTime = xTaskGetTickCount();
    int sentCount = 0;
    int cleanupCounter = 0;

    while (1) {
        // Check for incoming hello packets
        int packetSize = udp.parsePacket();
        if (packetSize > 0) {
            // Any incoming packet registers the client
            udpAddClient(udp.remoteIP(), udp.remotePort());
            // Drain the packet
            while (udp.available()) udp.read();
        }

        // Generate test data if in test mode
        if (nmea::isTestMode()) {
            nmea::generateTestData();
        }

        // Send to all active clients
        bool stalled = nmea::messageStall(2000);
        if (!stalled || nmea::isTestMode()) {
            int len = nmea::boatStateEncode(msgBuffer, nmea::boatState);
            memcpy(msgBuffer + len, terminator, 3);

            for (int i = 0; i < MAX_UDP_CLIENTS; i++) {
                if (udpClients[i].active) {
                    udp.beginPacket(udpClients[i].ip, udpClients[i].port);
                    udp.write(msgBuffer, len + 3);
                    udp.endPacket();
                }
            }
            sentCount++;

            if (sentCount % 40 == 0) {
                LOGF_INFO("UDP: sent=%d clients=%d heap=%d", sentCount, udpClientCount, ESP.getFreeHeap());
            }
        }

        // Cleanup stale clients every ~2 seconds
        cleanupCounter++;
        if (cleanupCounter >= 20) {
            udpCleanupClients();
            cleanupCounter = 0;
        }

        vTaskDelayUntil(&lastRunTime, 100 / portTICK_PERIOD_MS);  // 10Hz for responsiveness
    }
}

// create a data server task on port 100, which sends nmea boatstate
void boatStateTask(void* pvParameters) {
    LOG_INFO("dataServerTask started");
    WiFiServer server(TCP_PORT);
    server.begin();
    // Setup the data buffer, header and terminator
    unsigned char msgBuffer[128];
    unsigned char terminator[3] = {0xff, 0xff, '\n'};  // Terminator
    while(1) {
        WiFiClient client = server.available();
        if (client) {
            TickType_t lastRunTime = xTaskGetTickCount();
            RunningAverage loopTimer(0.95);
            int sentCount = 0;
            LOG_INFO("New client connected.");
            // to store the running average of the loop time
            while(client.connected()) {
                // If there is no nmea message for 2 seconds, pause the boatState data stream
                if (nmea::messageStall(2000)) {
                    // led1->enable(false);
                    LOG_ERROR("nmea::messageStall() detected.  Pausing data stream.");
                } else {
                    // led1->enable(true);
                    int len = nmea::boatStateEncode(msgBuffer, nmea::boatState);
                    memcpy(msgBuffer+len, terminator, 3);  // Add the terminator
                    client.write(msgBuffer, len + 3);
                    sentCount++;
                    loopTimer.event();
                    if (sentCount % 30 == 0) {
                        LOGF_DEBUG("Sent %d messages.  Average loop time: %d", sentCount, loopTimer.getAverage());
                    }
                }
                vTaskDelayUntil(&lastRunTime, 100 / portTICK_PERIOD_MS);
            }
            LOGF_INFO("Client disconnected. Sent %d messages.", sentCount);
        }
        vTaskDelay(200 / portTICK_PERIOD_MS);
    }
    vTaskDelete(NULL);
}


// In the background flush the trace and log file every N seconds
void traceLogFlushTask(void* pvParameters) {
    LOG_INFO("traceLogFlushTask started");
    while(1) {
        // LOG_FILE_FLUSH();
        // flush the logging stream
        if (loggingStream) {
            loggingStream.flush();
        }
        vTaskDelay(5000 / portTICK_PERIOD_MS);
    }
}

#ifdef CORES3
void receiveNmeaMessage(void *parameter) {
    int lastLogTime = millis();
    LOG_INFO("receiveNmeaMessage started");
    delay(1000);
    int lastMsgCount = 0;
    while (true) {
        if (millis() - lastLogTime > 1000) {
            PRINTF("MSG %d time %d\n", N2K_MSG_COUNT - lastMsgCount, millis());
            lastMsgCount = N2K_MSG_COUNT;
            lastLogTime = millis();
        }
        esp_err_t result = ESP32_CAN_read_frame();
        if (result == ESP_OK) { 
            N2K_MSG_COUNT++;
        }
    }
}
#endif

// The task function that uses the parameters
void ledTask(void *pvParameters) {
    LED* led = (LED *)pvParameters;

    while (1) {
        led->toggle();
    }
}

bool sdcardSetup = false;

void setup() {
    BOARD_BEGIN();

    Display::setup(DISPLAY_SERIAL, DISPLAY_SERIAL);
    Display::setLogLevel(INFO);

    displaySetup();
    LOG_INFO("DISPLAY Setup Done!");

#ifdef CORES3
    // Configure the AXP2101 so boat power (PwrCAN) is primary with automatic
    // failover to the optional M5GO Bottom3 battery. Safe with no cell present.
    power::setup();
    LOG_INFO("POWER Setup Done!");
#endif

    nmea::setup();
    sdcardSetup = sdcard::setup(5);  // initialize SD card

    String traceFile = uniqueDefaultLogFileName(".trace") + ".trace";

    // LOG_ATTACH_FS_MANUAL(SDCARD, traceFile, FILE_WRITE);

    bool isConnected = false;
    bool touchPressed = false;
    bool testModeRequested = false;

    displayString("Tap=AP, Hold=Test", 10, 10, true);
    delay(500);
    int touchDuration = 0;
    for (int i = 0; i < 15; i++) {  // 3 seconds total window
        M5.update();
        auto touchDetail = M5.Touch.getDetail();
        LOGF_INFO("Touch %d %04d  ", i, touchDetail.state);
        if (touchDetail.state) {
            touchDuration++;
            if (touchDuration == 1) {
                LOG_INFO("Touch detected...");
                touchPressed = true;
            }
            if (touchDuration >= 10) {  // 2+ seconds = test mode
                LOG_INFO("LONG TOUCH - Test mode!");
                testModeRequested = true;
                break;
            }
        } else if (touchPressed && touchDuration > 0 && touchDuration < 10) {
            // Short touch released - AP mode
            LOG_INFO("SHORT TOUCH - AP mode");
            break;
        }
        delay(200);
    }

    // Enable test mode if requested
    if (testModeRequested) {
        nmea::enableTestMode(true);
        displayString("TEST MODE", 10, 50, false);
        delay(1000);
    }

    LOG_INFO("touchPressed: %d", touchPressed);

    if (!touchPressed) {
        // isConnected = webServer::wifiJoinNetwork(WIFI_SSID, WIFI_PASSWORD);
        isConnected = webServer::wifiJoinNetwork(WIFI_SSID, WIFI_PASSWORD);
    }

    if (isConnected) {
        LOG_INFO("Connected to WiFi network:", WIFI_SSID);
    } else {
        LOG_ERROR("Failed to connect to WiFi network:", WIFI_SSID);
        LOG_ERROR("Creating WiFi Access Point network:", WIFI_MYAP);
        isConnected = webServer::wifiCreateAccessPoint(WIFI_MYAP, WIFI_MYAP_PASSWORD);
        if (isConnected) {
            LOG_INFO("Created WiFi Access Point network:", WIFI_MYAP);
        } else {
            LOG_ERROR("Failed to create WiFi Access Point network:", WIFI_MYAP);
        }
    }

    // UDP task - sends boat state to iOS app
    xTaskCreatePinnedToCore(boatStateUdpTask, "boatStateUdpTask", 8 * 1024, NULL, 3, NULL, 1);    

    xTaskCreatePinnedToCore(nmeaDataLoggerLoopTask, "data logger loop", 8 * 1024, NULL, 5, NULL, 1);  // was pri 0, core 0

    xTaskCreatePinnedToCore(traceLogFlushTask, "traceFlushTask", 8 * 1024, NULL, 5, NULL, 1);  // was pri 0, core 0

    // xTaskCreatePinnedToCore(idleTask, "idleTask", 4 * 1024, NULL, 1, NULL, 1);  

    // if not CORES3
    #ifndef CORES3
        led1 = new LED(19, 30, 300);
        xTaskCreatePinnedToCore(ledTask, "ledTask1", 1 * 1024, led1, 0, NULL, 1);

        led2 = new LED(18, 30, 300);
        xTaskCreatePinnedToCore(ledTask, "ledTask2", 1 * 1024, led2, 0, NULL, 1);
        led2->enable(sdcardSetup);  // Flash the LED if the SD card is working
    #endif

#ifdef CORES3
    LOG_INFO("Starting receiveNmeaMessage task");
    xTaskCreatePinnedToCore(receiveNmeaMessage,    /* Function to implement the task */
                            "Task3",               /* Name of the task */
                            4096,                  /* Stack size in words */
                            NULL,                  /* Task input parameter */
                            10, /* Priority of the task */
                            NULL,                  /* Task handle. */
                            1);                    /* Core where the task should run */

    M5.Display.fillScreen(TFT_BLACK);                            
#endif                        
}

void displaySetup() {
    M5.Display.setTextSize(2); // Adjust text size as needed
    M5.Display.setTextColor(TFT_WHITE, TFT_BLACK); // White text on black background
    M5.Display.setTextDatum(TL_DATUM); // Top-left alignment
    M5.Display.fillScreen(TFT_BLACK);    
}

void displayString(const String& str, int x, int y, bool clear) {
    if (clear) {
        M5.Display.fillScreen(TFT_BLACK);
    }
    M5.Display.drawString(str, x, y);
}

// Function to display the BoatState on the screen
void displayBoatState(const nmea::tBoatState& state) {
  // Clear the screen
  // M5.Display.fillScreen(TFT_BLACK);

  // Set text properties
  
  // Define starting position
  int x = 10; // X-coordinate
  int y = 10; // Y-coordinate
  const int lineHeight = 30; // Height of each line
  const int FIELD_WIDTH = 150;

  // Display each field name and value
  M5.Display.drawString("SOG: " + String(state.sog), x, y);
  y += 0; x += FIELD_WIDTH;
  M5.Display.drawString("COG: " + String(state.cog), x, y);
  
  y += lineHeight; x = 10;
  M5.Display.drawString("HDG: " + String(state.hdg), x, y);
  y += 0; x += FIELD_WIDTH;
  M5.Display.drawString("SPD: " + String(state.spd), x, y);
  
  y += lineHeight; x = 10;
  M5.Display.drawString("AWS: " + String(state.aws), x, y);
  y += 0; x += FIELD_WIDTH;
  M5.Display.drawString("AWA: " + String(state.awa), x, y);

  y += lineHeight; x = 10;
  M5.Display.drawString("DEP: " + String(state.depth), x, y);
  y += 0; x += FIELD_WIDTH;
  M5.Display.drawString("TWD: " + String(state.twd), x, y);

  y += lineHeight; x = 10;
  M5.Display.drawString("TWA: " + String(state.twa), x, y);
  y += 0; x += FIELD_WIDTH;
  M5.Display.drawString("TWS: " + String(state.tws), x, y);

  // display current log file
  y += lineHeight; x = 10;
  M5.Display.drawString(loggingFile, x, y);

    // display the ip address (left) + the N2K message rate (right-justified)
  y += lineHeight; x = 10;
  if (WiFi.getMode() == WIFI_AP) {
    M5.Display.drawString("AP IP: " + WiFi.softAPIP().toString(), x, y);
  } else {
    M5.Display.drawString("IP: " + WiFi.localIP().toString(), x, y);
  }
  // N2K message rate, right-justified on the IP row. Clear its box first so the
  // variable-width number leaves no stale pixels (the screen is not cleared each
  // frame), then draw with a right datum and restore the default left datum.
  M5.Display.fillRect(240, y, M5.Display.width() - 240, lineHeight, TFT_BLACK);
  M5.Display.setTextDatum(TR_DATUM);
  M5.Display.drawString(String(N2K_MSG_RATE, 1), M5.Display.width(), y);
  M5.Display.setTextDatum(TL_DATUM);

  // Bottom row: power/battery status (or TEST MODE indicator). Clear the whole
  // row first so the right-justified / variable-width fields don't leave stale
  // pixels.
  y += lineHeight; x = 10;
  M5.Display.fillRect(0, y, M5.Display.width(), lineHeight, TFT_BLACK);
  if (nmea::isTestMode()) {
    M5.Display.setTextColor(TFT_YELLOW, TFT_BLACK);
    M5.Display.drawString("** TEST MODE **", x, y);
    // Show UDP client count
    x += FIELD_WIDTH + 20;
    M5.Display.drawString("UDP:" + String(udpClientCount), x, y);
    M5.Display.setTextColor(TFT_WHITE, TFT_BLACK);
  } else {
    power::Status ps = power::read();
    power::logSourceChange(ps);
    // Left: colored source + optional Bottom3 battery %. A boat-power loss is
    // the logger's alarm state (now on reserve) -> red; low reserve on boat
    // power -> orange; otherwise green. Right column: voltages, or on battery
    // the time remaining until the graceful auto-shutdown ("off M:SS").
    uint16_t pcol; String pstr; String det;
    if (!ps.onBoat) {
      pcol = TFT_RED;
      if (ps.hasBatt) {
        pstr = "BATT " + String(ps.level) + "%";
        uint32_t remain = (batteryMs < power::BATTERY_SHUTDOWN_MS)
                        ? (power::BATTERY_SHUTDOWN_MS - batteryMs) : 0;
        uint32_t rs = remain / 1000;
        char buf[16];
        snprintf(buf, sizeof(buf), "off %lu:%02lu",
                 (unsigned long)(rs / 60), (unsigned long)(rs % 60));
        det = buf;
      } else {
        pstr = "NO POWER?";
      }
    } else if (ps.hasBatt && ps.level >= 0 && ps.level < 20) {
      pcol = TFT_ORANGE;
      pstr = "BOAT " + String(ps.level) + "% lo";
      det  = String(ps.vbat, 2) + "V in" + String(ps.vbus, 1) + "V";
    } else {
      pcol = TFT_GREEN;
      if (ps.hasBatt) {
        pstr = "BOAT " + String(ps.level) + "% " + power::chgString(ps.chg);
        det  = String(ps.vbat, 2) + "V in" + String(ps.vbus, 1) + "V";
      } else {
        pstr = "BOAT (no bat)";
        det  = "in " + String(ps.vbus, 1) + "V";
      }
    }
    M5.Display.setTextColor(pcol, TFT_BLACK);
    M5.Display.drawString(pstr, x, y);
    M5.Display.setTextDatum(TR_DATUM);
    M5.Display.drawString(det, M5.Display.width(), y);  // right-justified
    M5.Display.setTextDatum(TL_DATUM);
    M5.Display.setTextColor(TFT_WHITE, TFT_BLACK);  // restore for next frame's fields
  }

}


#ifdef CORES3
// Close the log cleanly and power off. Invoked after the logger has run on the
// reserve battery for power::BATTERY_SHUTDOWN_MS (boat power lost that long).
// Does not return: the AXP2101 cuts power. Re-applying boat power reboots us.
void gracefulShutdown() {
    LOGF_WARN("Battery shutdown: boat power lost for >%lus -- closing logs, powering off",
              (unsigned long)(power::BATTERY_SHUTDOWN_MS / 1000));
    // Stop the background logger task writing, then flush + close the file so the
    // SD FAT isn't left dirty (same disable-then-close idiom as swapDataLogFile).
    nmea::LoggingStream = NULL;
    delay(50);
    if (loggingStream) {
        loggingStream.flush();
        loggingStream.close();
    }
    // Tell the user, then power down.
    M5.Display.fillScreen(TFT_BLACK);
    M5.Display.setTextColor(TFT_RED, TFT_BLACK);
    M5.Display.setTextDatum(TL_DATUM);
    M5.Display.drawString("BOAT POWER LOST", 10, 90);
    M5.Display.drawString("Logs saved.", 10, 120);
    M5.Display.drawString("Shutting down...", 10, 150);
    delay(2000);
    power::shutdown();  // does not return
}
#endif

void loop() {
#ifdef CORES3
    // Graceful-shutdown timer: boat power is the reliable source, so a sustained
    // run on the reserve battery means the boat is off / we're unplugged. After
    // power::BATTERY_SHUTDOWN_MS continuously on battery, save and power down.
    {
        power::Status ps = power::read();
        batteryMs = power::onBatteryElapsedMs(ps);
        if (batteryMs >= power::BATTERY_SHUTDOWN_MS) {
            gracefulShutdown();  // does not return
        }
    }
#endif
    if (sdcardSetup && sdcard::isHealthy()) {
        // led2->enable(true);
    } else {
        LOG_ERROR("SD card not healthy.");
        // led2->enable(false);
        sdcardSetup = sdcard::setup(1);
        //led2->enable(sdcardSetup);
    }
    displayBoatState(nmea::boatState);
    // compute message rate
    double rate = (N2K_MSG_COUNT - N2K_PREV_MSG_COUNT) / 0.2;
    N2K_PREV_MSG_COUNT = N2K_MSG_COUNT;
    const double ALPHA = 0.95;
    N2K_MSG_RATE = ALPHA * N2K_MSG_RATE + (1 - ALPHA) * rate;
    delay(200);
}