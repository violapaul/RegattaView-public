#pragma once

// If we are cores3, which has a display
#if defined(CORES3)

#include <M5Unified.h>
#define BOARD_BEGIN() do { \
    auto cfg = M5.config(); \
    M5.begin(cfg); \
} while(0)

#else

#include <Arduino.h>
#define BOARD_BEGIN() do { \
} while(0)

#endif


