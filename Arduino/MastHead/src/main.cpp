#include <AS5600.h>
#include <Arduino.h>

#include "ICM_20948.h" 

#include <FS.h>
#include <ctime>
#include "WiFiClient.h"
#include "WiFiUdp.h"

#include "display.hpp"

#include "loopTimer.hpp"
#include "webServer.hpp"
#include "windex.hpp"
#include "led.hpp"
#include "ahrs.hpp"

#define TEXT_PORT 13579
#define MSG_PORT 13580

#define I2C_SPEED 100000L

#ifdef seeeds3
#define I2C_SCL1 6
#define I2C_SDA1 5

#define I2C_SCL2 2
#define I2C_SDA2 1
#else  // #ifdef ARDUINO_Node32s
#define I2C_SDA1 21
#define I2C_SCL1 22

#define I2C_SCL2 5
#define I2C_SDA2 18
#endif

// Set these to your desired credentials.
#include "wifi_credentials.hpp"

#define WIFI_MYAP "Esp32AP"


// For the most reliable interaction with the SHTP bus, we need
// to use hardware reset control, and to monitor the H_INT pin.
// The H_INT pin will go low when its okay to talk on the SHTP bus.
// Note, these can be other GPIO if you like.
// Define as -1 to disable these features.
// #define BNO08X_INT  A4
#define BNO08X_INT -1
// #define BNO08X_RST  A5
#define BNO08X_RST -1

#define BNO08X_ADDR 0x4B  // SparkFun BNO08x Breakout (Qwiic) defaults to 0x4B
// #define BNO08X_ADDR 0x4A // Alternate address if ADR jumper is closed

WiFiServer server(80);
uint8_t ROTARY_ENCODER_I2C_ADDR = 0x36;
AS5600L AWS_ENCODER(ROTARY_ENCODER_I2C_ADDR, &Wire);
AS5600L AWA_ENCODER(ROTARY_ENCODER_I2C_ADDR, &Wire1);

ICM_20948_I2C IMU;  // Otherwise create an ICM_20948_I2C object

#define AD0_VAL 1  // Default value of the jumper 

windex::tWindexState windexState;

void setupEncoder(AS5600L& as) {
    LOG_INFO("Connecting to Encoder");
    as.begin();                          //  set direction pin.
    as.setAddress(ROTARY_ENCODER_I2C_ADDR);          //  AS5600L has address
    as.setDirection(AS5600_CLOCK_WISE);  //  default, just be explicit.
    int b = as.isConnected();
    LOG_INFO("Connect: %d", b);
}

// Here is where you define the sensor outputs you want to receive
void setupIMU(void) {
    bool initialized = false;
    while (!initialized) {

        IMU.begin(Wire, AD0_VAL);

        LOG_INFO("Initialization of the sensor returned: %s", IMU.statusString());
        if (IMU.status != ICM_20948_Stat_Ok) {
            LOG_WARN("Trying again...");
            delay(500);
        } else {
            initialized = true;
        }
    }

  ahrs::setup(IMU);
}


#define SENSOR_PERIOD 50


void readAWATask(void* pvParameters) {
    LOG_INFO("started");
    TickType_t lastRunTime = xTaskGetTickCount();
    while (1) {
        // LOG_INFO("Reading");
        float angle = AWA_ENCODER.readAngle() / 4096.0 * 360.0;
        float speed = AWA_ENCODER.getAngularSpeed();
        //LOG_INFO("Angle: %6.2f    Speed: %7.2f", angle, speed);
        windexState.rawAwaAngle = angle;
        windexState.rawAwaSpeed = speed;
        vTaskDelayUntil(&lastRunTime, SENSOR_PERIOD / portTICK_PERIOD_MS);
    }
    vTaskDelete(NULL);
}

void readAWSTask(void* pvParameters) {
    LOG_INFO("started");
    TickType_t lastRunTime = xTaskGetTickCount();
    while (1) {
        windexState.rawAwsAngle = AWS_ENCODER.readAngle() / 4096.0 * 360.0;
        windexState.rawAwsSpeed = AWS_ENCODER.getAngularSpeed();
        vTaskDelayUntil(&lastRunTime, SENSOR_PERIOD / portTICK_PERIOD_MS);
    }
    vTaskDelete(NULL);
}

// Update the roll, pitch, and yaw values in the windexState, only call this when needed because it
// is a bit expensive.
void updateRPZ() {
    float roll, pitch, yaw;
    ahrs::rollPitchYaw(roll, pitch, yaw);
    // Serial.printf("UP Roll: %7.2f  Pitch: %7.2f  Yaw: %7.2f\n", roll, pitch, yaw);
    windexState.roll = roll;
    windexState.pitch = pitch;
    windexState.yaw = yaw;
}


void readOrientationTask(void* pvParameters) {
    LOG_INFO("started");
    TickType_t lastRunTime = xTaskGetTickCount();
    while (1) {
        if (ahrs::updateState(IMU)) {
            updateRPZ();
            vTaskDelayUntil(&lastRunTime, 10 / portTICK_PERIOD_MS);
        } else {
            LOG_WARN("Waiting for data");
            vTaskDelayUntil(&lastRunTime, 100 / portTICK_PERIOD_MS);
        }
    }
    vTaskDelete(NULL);
}


void loopTestTask(void* pvParameters) {
    LOG_INFO("started");
    TickType_t lastRunTime = xTaskGetTickCount();
    while (1) {
        ahrs::loop(IMU);
        vTaskDelayUntil(&lastRunTime, 10 / portTICK_PERIOD_MS);
    }
    vTaskDelete(NULL);
}


int sprintEncoder(AS5600L& as, char* buffer) {
    // Read the angle in degrees.
    float angle = as.readAngle() / 4096.0 * 360.0;
    float speed = as.getAngularSpeed();
    // format angle with 3 digits and 2 decimal.
    return sprintf(buffer, "Angle: %6.2f    Speed: %7.2f\n", angle, speed);
}

void sensorMsgTask(void* pvParameters) {
    LOG_INFO("started");
    WiFiServer server(MSG_PORT);
    server.begin();
    // Setup the data buffer, header and terminator
    unsigned char msgBuffer[256];
    unsigned char terminator[3] = {0xff, 0xff, '\n'};  // Terminator
    bool ledstate = false;
    led(ledstate);
    TickType_t lastRunTime = xTaskGetTickCount();
    while (1) {
        WiFiClient client = server.available();
        if (client) {
            TickType_t lastRunTime = xTaskGetTickCount();
            loopTimer::RunningStats timer(0.95, 100);
            int sentCount = 0;
            LOG_INFO("New client connected.");
            // to store the running average of the loop time
            while (client.connected()) {
                int len = windex::windexEncode(msgBuffer, windexState);
                memcpy(msgBuffer+len, terminator, 3);  // Add the terminator
                client.write(msgBuffer, len + 3);
                sentCount++;
                timer.event();
                if (sentCount % 30 == 0) {
                    LOG_INFO("Sent %d messages.  Times: %d", sentCount, timer.getAverage(), timer.getMin(), timer.getMax());
                    // Show stack space
                    LOG_INFO("Stack: %d", uxTaskGetStackHighWaterMark(NULL));
                }
                ledstate = !ledstate;
                led(ledstate);
                vTaskDelayUntil(&lastRunTime, 100 / portTICK_PERIOD_MS);
            }
            LOG_INFO("Client disconnected. Sent %d messages.", sentCount);
        }
        vTaskDelayUntil(&lastRunTime, 200 / portTICK_PERIOD_MS);
    }
    vTaskDelete(NULL);
}


void sensorDisplayTask(void* pvParameters) {
    LOG_INFO("started");
    WiFiServer server(TEXT_PORT);
    server.begin();
    // Setup the data buffer, header and terminator
    char msgBuffer[128];
    unsigned char terminator[3] = {0xff, 0xff, '\n'};  // Terminator
    bool ledstate = false;
    led(ledstate);
    TickType_t lastRunTime = xTaskGetTickCount();
    while (1) {
        WiFiClient client = server.available();
        if (client) {
            TickType_t lastRunTime = xTaskGetTickCount();
            loopTimer::RunningStats timer(0.95, 100);
            int sentCount = 0;
            LOG_INFO("New client connected.");
            // to store the running average of the loop time
            while (client.connected()) {
                int len = sprintf(msgBuffer, "Count: %7d\n", sentCount);
                client.write(msgBuffer, len);
                // send the time since boot
                len = sprintf(msgBuffer, "Raw awa angle: %7.2f  speed: %7.2f\n", windexState.rawAwaAngle, windexState.rawAwaSpeed);
                client.write(msgBuffer, len);
                len = sprintf(msgBuffer, "Raw aws angle: %7.2f  speed: %7.2f\n", windexState.rawAwsAngle, windexState.rawAwsSpeed);
                client.write(msgBuffer, len);
                len = sprintf(msgBuffer, "Roll %7.2f  Pitch %7.2f  Yaw %7.2f\n", windexState.roll, windexState.pitch, windexState.yaw);
                client.write(msgBuffer, len);
                sentCount++;
                timer.event();
                if (sentCount % 30 == 0) {
                    LOG_INFO("Sent %d messages. Loop time: ", sentCount, timer.getAverage(), timer.getMin(), timer.getMax());
                }
                ledstate = !ledstate;
                led(ledstate);
                vTaskDelayUntil(&lastRunTime, 100 / portTICK_PERIOD_MS);
            }
            LOG_INFO("Client disconnected. Sent %d messages.", sentCount);
        }
        vTaskDelayUntil(&lastRunTime, 200 / portTICK_PERIOD_MS);
    }
    vTaskDelete(NULL);
}

void setupWifi() {
    LOG_INFO("Connecting to WiFi");

    // join wifi
    webServer::wifiJoinNetwork(WIFI_SSID, WIFI_PASSWORD, 10, "wifiLEDTest");
    // create wifi
    // webServer::wifiCreateAccessPoint(WIFI_MYAP, WIFI_MYAP_PASSWORD, "wifiLEDTest");

    server.begin();
    LOG_INFO("Server started");
}


void setup() {

    pinMode(LED_BUILTIN, OUTPUT);

    Display::setup(DISPLAY_SERIAL, DISPLAY_SERIAL);
    Display::setLogLevel(INFO);

    // blink the light 8 times!
    for (int i = 0; i < 8; i++) {
        led(true);
        delay(200);
        led(false);
        delay(200);
    }

    LOGF_INFO("AS5600_LIB_VERSION: %s", AS5600_LIB_VERSION);

    Wire.begin(I2C_SDA1, I2C_SCL1, I2C_SPEED);
    Wire1.begin(I2C_SDA2, I2C_SCL2, I2C_SPEED);

    setupEncoder(AWA_ENCODER);
    setupEncoder(AWS_ENCODER);
    delay(1000);

    setupWifi();
    setupIMU();

    // Create the data server task
    xTaskCreatePinnedToCore(sensorDisplayTask, "displayTask", 4 * 1024, NULL, 1, NULL, 1);  // was pri 0, core 0 
    xTaskCreatePinnedToCore(sensorMsgTask, "msgTask", 4 * 1024, NULL, 1, NULL, 1);  // was pri 0, core 0   

    xTaskCreatePinnedToCore(readAWATask, "readAWATask", 4 * 1024, NULL, 2, NULL, 1);  // was pri 0, core 0
    xTaskCreatePinnedToCore(readAWSTask, "readAWSTask", 4 * 1024, NULL, 2, NULL, 1);  // was pri 0, core 0
    xTaskCreatePinnedToCore(readOrientationTask, "readOrientationTask", 4 * 1024, NULL, 2, NULL, 1);  // was pri 0, core 0
    // xTaskCreatePinnedToCore(loopTestTask, "loopTestTask", 4 * 1024, NULL, 2, NULL, 1);  // was pri 0, core 0
}

void loopEncoders() {
    char buffer[128];
    LOGF_INFO("AWA: %s", sprintEncoder(AWA_ENCODER, buffer));
    LOGF_INFO("AWS: %s", sprintEncoder(AWS_ENCODER, buffer));
    delay(100);
}

void loop() {
    // delay(1000);
    // ahrs::loop(IMU);
    delay(100);

    // Serial.printf("Roll: %7.2f  Pitch: %7.2f  Yaw: %7.2f\n", windexState.roll, windexState.pitch,
    // windexState.yaw);
    Serial.printf("%5.0f %5.0f %5.0f\n", windexState.yaw, windexState.pitch, windexState.roll);

    // loopEncoders();
}


//  -- END OF FILE --