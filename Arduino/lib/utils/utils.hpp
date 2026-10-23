#pragma once

#include "board.hpp"
#include "display.hpp"
#include <vector>

namespace utils {
    void readCommand(char* buf, int maxLen, int &len);
    bool readCommandInteractive(char* buf, int maxLen, int &len);
    bool matchPattern(const char *str, const char *pattern);
    std::vector<String> split(String str, String sep);
    String join(std::vector<String> strings, String sep);
    String unixtimeToFilename(time_t unixtime);
    void logDebug(const char *format, ...);
    void logInfo(const char *format, ...);
    void logError(const char *format, ...);
} // namespace utils
