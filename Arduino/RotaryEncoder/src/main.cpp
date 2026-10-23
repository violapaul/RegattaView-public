#include <Arduino.h>
#include "ICM_20948.h"

// I2C pins and settings from original main.cpp
#define I2C_SPEED 100000L
#define I2C_SCL1 2
#define I2C_SDA1 1

#define AD0_VAL 1  // Default value of the jumper

ICM_20948_I2C IMU;  // Create ICM_20948_I2C object

void setupIMU() {
    bool initialized = false;
    while (!initialized) {
        IMU.begin(Wire, AD0_VAL);
        
        Serial.print("Initialization of the sensor returned: ");
        Serial.println(IMU.statusString());
        
        if (IMU.status != ICM_20948_Stat_Ok) {
            Serial.println("Trying again...");
            delay(500);
        } else {
            initialized = true;
        }
    }
}

float xOffset = (-538 + -262) / 2;
float yOffset = (80 + -213) / 2;
float zOffset = (220 + 270) / 2;

// Scale scales to +1 and -1 give the offsets above
float xScale = 1 / (-538 - xOffset);
float yScale = 1 / (80 - yOffset);
float zScale = 1 / (220 - zOffset);

// Structure to hold magnetometer data and averages
struct MagData {
    float sX = 0;
    float sY = 0; 
    float sZ = 0;
    float lX = 0;
    float lY = 0;
    float lZ = 0;
    // Store the max and min values
    float maxX = 0;
    float minX = 0;
    float maxY = 0;
    float minY = 0;
    float maxZ = 0;
    float minZ = 0;

    float vecX() {
        return (sX - xOffset) * xScale;
    }   
    float vecY() {
        return (sY - yOffset) * yScale;
    }   
    float vecZ() {
        return (sZ - zOffset) * zScale;
    }
    // orientation starts with dot and then some trig
    float orientation() {
        float deg = atan2(vecY(), vecX()) * 180.0f / PI;
        if (deg < 0) deg += 360.0f;
        return deg;
    }
} magData;

// Task handle
TaskHandle_t magTaskHandle = NULL;

float square(float x) {
    return x*x;
}

// Magnetometer averaging task
void magAveragingTask(void *parameter) {
    const float sBase = 0.03f;  // More weight on recent readings
    const float lBase = 0.001f;   // More weight on history
    float s = sBase;  // More weight on recent readings
    float l = lBase;   // More weight on history
    
    while(true) {
        if (IMU.dataReady()) {
            IMU.getAGMT();
            int16_t magX = IMU.agmt.mag.axes.x;
            int16_t magY = IMU.agmt.mag.axes.y;
            int16_t magZ = IMU.agmt.mag.axes.z; 
            
            // Update short term averages with exponential moving average

            float diff = sqrt(sq(magData.sX - magX) + sq(magData.sY - magY) + sq(magData.sZ - magZ));
            bool fast = (diff > 20) || (millis() < 10000);
            s = fast ? 20*sBase : sBase;
            magData.sX = (1.0f - s) * magData.sX + s * magX;
            magData.sY = (1.0f - s) * magData.sY + s * magY;
            magData.sZ = (1.0f - s) * magData.sZ + s * magZ;
            
            // Update long term averages with exponential moving average
            diff = sqrt(sq(magData.lX - magX) + sq(magData.lY - magY) + sq(magData.lZ - magZ));
            fast = (diff > 20) || (millis() < 10000);
            l = fast ? 20*lBase : lBase;
            magData.lX = (1.0f - l) * magData.lX + l * magX;
            magData.lY = (1.0f - l) * magData.lY + l * magY;
            magData.lZ = (1.0f - l) * magData.lZ + l * magZ;

            // Initialize max/min values with raw data if we just started
            if (millis() < 5000) {
                magData.maxX = magData.sX;
                magData.minX = magData.sX;
                magData.maxY = magData.sY;
                magData.minY = magData.sY;
                magData.maxZ = magData.sZ;
                magData.minZ = magData.sZ;
            } else {

                // Update max and min values
                magData.maxX = max(magData.maxX, magData.lX);
                magData.minX = min(magData.minX, magData.lX);
                magData.maxY = max(magData.maxY, magData.lY);
                magData.minY = min(magData.minY, magData.lY);
                magData.maxZ = max(magData.maxZ, magData.lZ);
                magData.minZ = min(magData.minZ, magData.lZ);
            }
        }
        vTaskDelay(pdMS_TO_TICKS(10)); // 100Hz sampling
    }
}



void setup() {
    Serial.begin(115200);
    while (!Serial) {
        delay(10);
    }
    
    Serial.println("Starting IMU test...");
    
    Wire.begin(I2C_SDA1, I2C_SCL1, I2C_SPEED);
    delay(100);
    
    setupIMU();

    delay(100);

    // Create the task in setup()
    xTaskCreate(
        magAveragingTask,    // Task function
        "MagAverage",        // Name for debugging
        4096,               // Stack size
        NULL,               // Parameters
        1,                  // Priority
        &magTaskHandle      // Task handle
);

}

void loop() {
        
        // Print magnetometer values with fixed width fields
        Serial.printf("R X: %6d Y: %6d Z: %6d ", IMU.agmt.mag.axes.x, IMU.agmt.mag.axes.y, IMU.agmt.mag.axes.z);
        Serial.printf(" S X: %8.2f Y: %8.2f Z: %8.2f ", magData.sX, magData.sY, magData.sZ);
        Serial.printf(" L X: %8.2f Y: %8.2f Z: %8.2f ", magData.lX, magData.lY, magData.lZ);
        Serial.printf(" Max X: %8.2f Y: %8.2f Z: %8.2f  Min X: %8.2f Y: %8.2f Z: %8.2f ", 
            magData.maxX, magData.maxY, magData.maxZ, 
            magData.minX, magData.minY, magData.minZ);
        Serial.printf(" Orientation: %8.2f\n", magData.orientation());
    delay(100);  // Small delay between readings
}