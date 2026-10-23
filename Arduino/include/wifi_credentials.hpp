#pragma once
// Per-device values are generated locally by Scripts/provision_wifi.py.
#if __has_include("wifi_credentials.local.hpp")
#include "wifi_credentials.local.hpp"
#endif
#ifndef WIFI_SSID
#define WIFI_SSID ""
#endif
#ifndef WIFI_PASSWORD
#define WIFI_PASSWORD ""
#endif
#ifndef WIFI_MYAP_PASSWORD
#define WIFI_MYAP_PASSWORD ""
#endif
// Empty configuration keeps radio access disabled; logging still works.
