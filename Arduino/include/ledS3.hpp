

#include <LiteLED.h>

// Choose the LED type from the list below.
// Comment out all but one LED_TYPE.
// #define LED_TYPE        LED_STRIP_WS2812
#define LED_TYPE LED_STRIP_SK6812
#define LED_TYPE_IS_RGBW 0  // if the LED is an RGBW type, change the 0 to 1
#define LED_GPIO 38         // change this number to be the GPIO pin connected to the LED

// pick the colour you want from the list here and change it in setup()
static const crgb_t L_RED = 0xff0000;
static const crgb_t L_GREEN = 0x00ff00;
static const crgb_t L_BLUE = 0x0000ff;
static const crgb_t L_WHITE = 0xe0e0e0;

// Create a class that can be used to control the LED
class LEDControl {
   public:
    LEDControl(int brightness, crgb_t color) : _brightness(brightness), _color(color) {
        // Create the LED object
        _led = new LiteLED(LED_TYPE, LED_TYPE_IS_RGBW);
        _led->begin(LED_GPIO, 1);
        _led->brightness(_brightness, true);
        _led->setPixel(0, _color);
        _isOn = true;
    }

    void setColour(crgb_t color) {
        _color = color;
        _led->setPixel(0, _color, true);
    }

    void off() {
        _led->brightness(0, true);
        _isOn = false;
    }

    void on() {
        _led->brightness(_brightness, true);
        _isOn = true;
    }

    void toggle() {
        if (_isOn) {
            off();
        } else {
            on();
        }
    }

   private:
    LiteLED* _led;
    int _brightness = 100;
    crgb_t _color = L_BLUE;
    bool _isOn = false;
};

class LED {
   public:
    LED(int gpio, int onTime, int offTime) : _gpio(gpio), _onTime(onTime), _offTime(offTime) { init(); }

    void init() {
        pinMode(_gpio, OUTPUT);
        off();
    }

    void enable(bool enable) {
        _enable = enable;
        if (!_enable) {
            off();
        }
    }

    void setCycle(int onTime, int offTime) {
        _onTime = onTime;
        _offTime = offTime;
    }

    void on() {
        _isOn = true;
        // Tricky, the pin is th drain, so LOW is on
        digitalWrite(_gpio, LOW);
    }

    void off() {
        _isOn = false;
        // Tricky, the pin is th drain, so HIGH is off
        digitalWrite(_gpio, HIGH);
    }

    void toggle() {
        if (_enable) {
            if (_isOn) {
                off();
                vTaskDelay(_offTime);
            } else {
                on();
                vTaskDelay(_onTime);
            }
        } else {
            off();
            vTaskDelay(100);
        }
    }

   private:
    bool _enable = false;
    bool _isOn;
    int _gpio;
    int _onTime;
    int _offTime;
};
