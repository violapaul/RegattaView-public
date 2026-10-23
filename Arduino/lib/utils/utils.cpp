
#include "utils.hpp"

#include <ctime>

namespace utils {

void readCommand(char* buf, int maxLen, int& len) {
    int read = Serial.readBytesUntil('\n', buf, maxLen - 1);
    buf[read] = '\0';
    len = read;
}

// A function to read a command from the serial monitor

// A function to read a command from the serial monitor
bool readCommandInteractive(char* buf, int maxLen, int& len) {
    char* p = buf;
    // Loop until a new line is received
    while (true && p - buf < maxLen - 1) {
        // Check if there is data available from the serial monitor
        if (Serial.available() > 0) {
            char c = Serial.read();
            if (c == -1) {
                LOG_ERROR("Error reading from serial monitor, -1\n");
                return false;
            }
            Serial.write(c);
            if (c == '\r') {
                // Ignore the carriage return
                continue;
            }
            // Check if the character is a new line
            if (c == '\n') {
                // Break the loop
                // LOG_INFO("Breaking loop");
                break;
            } else {
                // Append the character to the command
                *p++ = c;
            }
        }
    }
    // Null terminate the command
    *p = '\0';
    len = p - buf;
    if (len = maxLen - 1) {
        LOG_WARN("Command too long\n");
        return false;
    }
    return true;
}

// function which matches a string if it ends in a given pattern
bool matchPattern(const char* str, const char* pattern) {
    // get the length of the string
    int len = strlen(str);
    // get the length of the pattern
    int patlen = strlen(pattern);
    // if the pattern is longer than the string, it cannot match
    if (patlen > len) {
        return false;
    }
    // compare the end of the string with the pattern
    return strcmp(str + len - patlen, pattern) == 0;
}

// split a string into a vector of strings based on a separator, remove the leading separator if it exists
std::vector<String> split(String str, String sep) {
    std::vector<String> result;
    int start = 0;
    int end = str.indexOf(sep);
    while (end >= 0) {
        if (start != end) {
            result.push_back(String(str.substring(start, end)));
        }
        start = end + sep.length();
        end = str.indexOf(sep, start);
    }
    if (start < str.length()) {
        result.push_back(String(str.substring(start)));
    }
    return result;
}

// join a vector of strings into a single string
String join(std::vector<String> strings, String sep) {
    String result = "";
    for (int i = 0; i < strings.size(); i++) {
        result += strings[i];
        if (i < strings.size() - 1) {
            result += sep;
        }
    }
    return result;
}


void logBase(const char* format, va_list args) {
    char buf[256];
    vsnprintf(buf, sizeof(buf), format, args);
    LOG_INFO(buf);
}

// simple logging function that works a bit like printf()
void logDebug(const char* format, ...) {
    va_list args;
    va_start(args, format);
    char buf[256];
    vsnprintf(buf, sizeof(buf), format, args);
    va_end(args);
    LOG_INFO(buf);
}

// simple logging function that works a bit like printf()
void logInfo(const char* format, ...) {
    va_list args;
    va_start(args, format);
    char buf[256];
    vsnprintf(buf, sizeof(buf), format, args);
    va_end(args);
    LOG_INFO(buf);
}

void logError(const char* format, ...) {
    va_list args;
    va_start(args, format);
    char buf[256];
    vsnprintf(buf, sizeof(buf), format, args);
    va_end(args);
    LOG_INFO(buf);
}

}  // namespace utils