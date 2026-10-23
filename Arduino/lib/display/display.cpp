#include "display.hpp"
#include <thread>

namespace Display {

// Defaults
int DEBUG_LEVEL = WARN;
int LOG_OUTPUT = DISPLAY_SERIAL;
int PRINT_OUTPUT = DISPLAY_SERIAL;

// Internal queue to prevent threads stomping on each other
std::queue<DisplayMessage> messageQueue;
std::mutex queueMutex;
bool isRunning = false;
TaskHandle_t displayThread = nullptr;

void setupSerial() {
    Serial.begin(DISPLAY_SERIAL_SPEED);
}

// Pick one of these to get some additional debug output (yes, you are debugging the logging library)
// #define DDEBUG(...) Serial.println(__VA_ARGS__)
#define DDEBUG(...) (void)0

#ifdef CORES3
M5Canvas *CANVAS = nullptr;

void createCanvas() {
    CANVAS = new M5Canvas(&M5.Display);
    DDEBUG("setupCanvas: Setting up canvas");
    CANVAS->setColorDepth(1);
    DDEBUG("setupCanvas: Setting up canvas");
    CANVAS->createSprite(M5.Display.width(), M5.Display.height());
    DDEBUG("setupCanvas: Created sprite");
    CANVAS->setPaletteColor(1, GREEN);
    DDEBUG("setupCanvas: Set palette color");
    CANVAS->setTextSize((float)CANVAS->width() / 160);
    DDEBUG("setupCanvas: Set text size");
    CANVAS->setTextScroll(true);
    DDEBUG("setupCanvas: Set text scroll");
    CANVAS->println("LCD is setup!");
    CANVAS->pushSprite(0, 0);
}    
#else
void createCanvas() {
    DDEBUG("createCanvas: WARNING No canvas on non-CoreS3 boards");
}
#endif


void setLogLevel(int level) {
    DEBUG_LEVEL = level;
}

void setup() {
#ifdef DISPLAY_DEBUG
    setupSerial();
#endif
    bool need_serial = (LOG_OUTPUT | PRINT_OUTPUT) & DISPLAY_SERIAL;
    bool need_lcd = (LOG_OUTPUT | PRINT_OUTPUT) & DISPLAY_LCD;
    if (need_serial) {
        setupSerial();
    }
    if (need_lcd) {
        createCanvas();
    }
    startDisplayThread();
}

void setup(int log_output, int print_output) {
    LOG_OUTPUT = log_output;
    PRINT_OUTPUT = print_output;
    setup();
}

bool canWriteToSerial() {
    return Serial && Serial.availableForWrite() > 0;
}

String spacePad(int x) {
    return (x < 10 ? "     " : x < 100 ? "    " : x < 1000 ? "   " : x < 10000 ? "  " : x < 100000 ? " " : "") + String(x);
}

void display(int output) {}

void println(const char *str) {
    display(PRINT_OUTPUT, str);
    display(PRINT_OUTPUT, "\n");
}

String generate_log_header(const int lvl) {
    String header;
    switch (lvl) {
        case ERROR: header = "[ERROR] "; break;
        case WARN: header = "[WARN] "; break;
        case INFO: header = "[INFO] "; break;
        case DEBUG: header = "[DEBUG] "; break;
        case TRACE: header = "[TRACE] "; break;
        default: header = ""; break;
    }
    return header;
}

// Display thread
void processMessages(void* parameter) {
    (void)parameter;
    while (isRunning) {
        DisplayMessage* msg = nullptr;
        {
            std::lock_guard<std::mutex> lock(queueMutex);
            if (!messageQueue.empty()) {
                msg = new DisplayMessage(messageQueue.front());
                messageQueue.pop();
            }
        }
        
        if (msg) {
            // Use existing display logic
            if (msg->output & DISPLAY_SERIAL) {
                if (canWriteToSerial()) {
                    Serial.print(msg->message.c_str());
                }
            }
#if defined(CORES3)
            if (msg->output & DISPLAY_LCD) {
                if (CANVAS) {
                    CANVAS->println(msg->message.c_str());
                    CANVAS->pushSprite(0, 0);
                }
            }
#endif
            delete msg;
        }
        delay(1);
    }
}

void startDisplayThread() {
    isRunning = true;
    xTaskCreatePinnedToCore(
        processMessages,    // Function to implement the task
        "DisplayThread",    // Name of the task
        4096,              // Stack size in words
        NULL,              // Task input parameter
        1,                 // Priority of the task
        &displayThread,     // Task handle
        0
    );
}

void stopDisplayThread() {
    isRunning = false;
    if (displayThread != nullptr) {
        vTaskDelete(displayThread);
        displayThread = nullptr;
    }
}

} // namespace Display
