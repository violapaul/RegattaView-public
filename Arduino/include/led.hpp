

#ifdef seeeds3
#define LED_HI LOW
#define LED_LO HIGH
// #define LED_BUILTIN 21 // seeed studio xiao
#else
#define LED_HI HIGH
#define LED_LO LOW
#define LED_BUILTIN \
    2  // Set the GPIO pin where you connected your test LED or comment this line out if your dev board has a built-in LED
#endif

void led(bool on) {
    if (on) {
        digitalWrite(LED_BUILTIN, LED_HI);
    } else {
        digitalWrite(LED_BUILTIN, LED_LO);
    }
}
