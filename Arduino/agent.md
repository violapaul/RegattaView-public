
File contains a stream of conciousness that a future agent may be able to decode!

# 2024 09 13 11:34:41

## Build output for esp32s3

Processing seeed_xiao_esp32s3 (board: seeed_xiao_esp32s3; platform: espressif32; framework: arduino)
-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
Verbose mode can be enabled via `-v, --verbose` option
CONFIGURATION: https://docs.platformio.org/page/boards/espressif32/seeed_xiao_esp32s3.html
PLATFORM: Espressif 32 (6.5.0) > Seeed Studio XIAO ESP32S3
HARDWARE: ESP32S3 240MHz, 320KB RAM, 8MB Flash

Advanced Memory Usage is available via "PlatformIO Home > Project Inspect"
RAM:   [=         ]  14.6% (used 47800 bytes from 327680 bytes)
Flash: [==        ]  22.4% (used 750049 bytes from 3342336 bytes)


Project inspect is pretty cool!  Takes a while to run




# 2024 09 15 16:38:50

Got the icm 20948 working.

- Calibrate
  - Gyro offsets
  - Find the scaling for the Accel and Mag.
- Then run AHRS.  (this is probably not a good idea if you don't have a fast processor).


Much more controllable than the fancier unit (BNO086, which has an CPU of some kind,
but does not have a clear calibration scheme).






