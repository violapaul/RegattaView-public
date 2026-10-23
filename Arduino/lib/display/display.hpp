#pragma once

#include "board.hpp"
#include <stdarg.h>
#include <queue>
#include <mutex>
#include <string>

namespace Display {

#define LOG_MACRO_APPEND_STR(s) LOG_HELPER_MACRO_APPEND_STR(s)
#define LOG_HELPER_MACRO_APPEND_STR(s) #s

#define LOG_SHORT_FILENAME (strrchr(__FILE__, '/') ? strrchr(__FILE__, '/') + 1 : strrchr(__FILE__, '\\') ? strrchr(__FILE__, '\\') + 1 : __FILE__)

String spacePad(int x);

#define LOG_PREAMBLE Display::spacePad(millis()), LOG_SHORT_FILENAME, LOG_MACRO_APPEND_STR(L.__LINE__), __func__, ":"

#define TRACE 0
#define DEBUG 1
#define INFO 2
#define WARN 3
#define ERROR 4

#define DISPLAY_NONE 0
#define DISPLAY_SERIAL 1
#define DISPLAY_LCD 2
#define DISPLAY_BOTH (DISPLAY_SERIAL | DISPLAY_LCD)

extern int DEBUG_LEVEL;
extern int LOG_OUTPUT;
extern int PRINT_OUTPUT;



#ifndef DISPLAY_SERIAL_SPEED
#define DISPLAY_SERIAL_SPEED 115200
#endif

// The interfaces for setting things up.
void setLogLevel(int level);
void setup();
void setup(int log_output, int print_output);

#ifdef CORES3
extern M5Canvas *CANVAS;
#endif

// Internal function 
bool canWriteToSerial();

// the display functions do the actual work of displaying messages. Use 
// the macros below.

template <typename T>
void displayOne(int output, T str) {
    if (output & DISPLAY_SERIAL) {
        if (canWriteToSerial()) {
            Serial.print(str);
        }
    }
#ifdef CORES3
    if (output & DISPLAY_LCD && CANVAS) {
        CANVAS->print(str);
        CANVAS->pushSprite(0, 0);
    }
#endif
}

// Recursive display function.  Displays one argument, then a space, then recursively displays the rest.
// Base case, if there is nothing to display, do nothing
void display(int output);

template <typename Head, typename... Tail>
void display(const int output, const Head& head, Tail&&... tail) {

    displayOne(output, head);
    if (sizeof...(tail) != 0)
        displayOne(output, " ");

    display(output, std::forward<Tail>(tail)...);
}

// Printing is meant for advice and UX.
void println(const char *str);

template<typename... Args>
void printf(const char* format, Args... args) {
    char buf[512];
    snprintf(buf, sizeof(buf), format, args...);
    display(PRINT_OUTPUT, buf);
}

template <typename... Args>
void print(Args&&... args) {
    display(PRINT_OUTPUT, std::forward<Args>(args)...);
}

// Logging is meant for debugging.  The UX is sort of annoying.
template<typename... Args>
void logPrintf(const char* format, Args... args) {
    char buf[512];
    snprintf(buf, sizeof(buf), format, args...);
    displayOne(LOG_OUTPUT, buf);
    display(LOG_OUTPUT, "\n");
}

String generate_log_header(const int lvl);

template <typename... Args>
void logPrefix(int level, Args&&... args) {
    String header = generate_log_header(level);
    displayOne(LOG_OUTPUT, header);
    display(LOG_OUTPUT, std::forward<Args>(args)...);
    displayOne(LOG_OUTPUT, " ");
}

template <typename... Args>
void logPrint(Args&&... args) {
    display(LOG_OUTPUT, std::forward<Args>(args)...);
    display(LOG_OUTPUT, "\n");
}

// These are the macros you should actually use.

#define PRINTF(format, ...) Display::printf(format, ##__VA_ARGS__)
#define PRINTLN(str) Display::println(str)
#define PRINT(...) Display::print(__VA_ARGS__)

#define LOG_BASE(level, ...) do { \
    if (level >= Display::DEBUG_LEVEL) { \
        Display::logPrefix(level, LOG_PREAMBLE); \
        Display::logPrint(__VA_ARGS__); \
    } \
} while(0)

#define LOGF_BASE(level, format, ...) do { \
    if (level >= Display::DEBUG_LEVEL) { \
        Display::logPrefix(level, LOG_PREAMBLE); \
        Display::logPrintf(format, ##__VA_ARGS__); \
    } \
} while(0)

#define LOG_TRACE(str, ...) LOG_BASE(TRACE, str, ##__VA_ARGS__)
#define LOGF_TRACE(format, ...) LOGF_BASE(TRACE, format, ##__VA_ARGS__)
#define LOG_DEBUG(str, ...) LOG_BASE(DEBUG, str, ##__VA_ARGS__)
#define LOGF_DEBUG(format, ...) LOGF_BASE(DEBUG, format, ##__VA_ARGS__)
#define LOG_INFO(str, ...) LOG_BASE(INFO, str, ##__VA_ARGS__)
#define LOGF_INFO(format, ...) LOGF_BASE(INFO, format, ##__VA_ARGS__)
#define LOG_WARN(str, ...) LOG_BASE(WARN, str, ##__VA_ARGS__)
#define LOGF_WARN(format, ...) LOGF_BASE(WARN, format, ##__VA_ARGS__)
#define LOG_ERROR(str, ...) LOG_BASE(ERROR, str, ##__VA_ARGS__) 
#define LOGF_ERROR(format, ...) LOGF_BASE(ERROR, format, ##__VA_ARGS__)

struct DisplayMessage {
    int output;
    String message;
    DisplayMessage(int o, const String& msg) : output(o), message(msg) {}
};

extern std::queue<DisplayMessage> messageQueue;
extern std::mutex queueMutex;
extern bool isRunning;

void processMessages();
void startDisplayThread();
void stopDisplayThread();

}