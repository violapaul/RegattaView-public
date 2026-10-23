#pragma once
//
// Optional M5GO Bottom3 battery support for the BoatBrain CoreS3 stack.
//
//   bottom : M5GO Bottom3   (LiPo battery -> AXP2101 BAT)   [OPTIONAL]
//   middle : PwrCAN 13.2     (9-24V boat power -> isolated 5V onto the M-Bus)
//   top    : M5Stack CoreS3  (ESP32-S3 + AXP2101 PMIC, runs this firmware)
//
// Power failover is AUTOMATIC in the AXP2101 power path: when the PwrCAN feeds
// 5V on the bus the logger runs off boat power and trickle-charges the Bottom3;
// when boat power drops the AXP2101 switches to the Bottom3 seamlessly with no
// reset and no firmware in the switching loop. This module's job is only to (a)
// configure the PMIC so the PwrCAN can actually act as the primary source and
// the cell can charge, and (b) report which source is live so a boat-power loss
// is visible on-screen and in the trace log -- which for a *logger* is exactly
// the moment it may be about to die.
//
// The battery is OPTIONAL. Every setting here is safe with no cell installed:
// `setExtOutput(false)` is correct either way, and `read()` reports `hasBatt =
// false` / `level = -1` (the existing CoreS3-only stack just shows "NO BAT").
//
// Distilled from the standalone Arduino/BatteryMonitor sketch and its bench
// findings (2026-06-09); see Arduino/BatteryMonitor/README.md for the full
// write-up. The entire PMIC API used here is present in M5Unified 0.1.17, the
// version BoatBrain already pins -- no library bump required.
//
// Hardware facts that shape this code:
//  - PwrCAN isolation = Mornsun F0505S-2WR3 = 2W => only ~400mA @ 5V for the
//    WHOLE stack. BoatBrain (WiFi AP + CAN read + SD logging) draws most of that
//    on its own, so charge current is capped hard (CHARGE_MA) and the Bottom3
//    acts mainly as a reserve + buffer cap; it may charge only slowly, or lean
//    on the cell under load. That is the intended "optional reserve" behaviour.
//  - The PwrCAN bus 5V registers as VBUS on the AXP2101, so isVBUS() == "boat
//    power present". VBUS gone => running on the Bottom3.
//  - The AXP2101 has NO battery-current ADC and the CoreS3 has no shunt, so
//    charge/discharge current in mA is simply not measurable here. We report
//    direction (charge/standby/discharge) from the charge-status register only.

#ifdef CORES3

#include "board.hpp"    // M5 / AXP2101 (via <M5Unified.h>)
#include "display.hpp"  // LOG_* / LOGF_* macros

namespace power {

// Cap battery charge current to protect the PwrCAN 2W (~400mA) isolated supply.
// 125mA is the lowest AXP2101 step; raise only with a beefier 5V feed.
static constexpr uint16_t CHARGE_MA = 125;

// The PwrCAN's isolated 5V sags under load -- below the AXP2101's default VBUS
// input-voltage limit (VINDPM), so the charger refuses to charge on boat power
// even though it charges fine off a stiff USB 5V. Lower VINDPM so the weaker bus
// rail is accepted as a charge source. Reg 0x15 low nibble: 0=3.88V .. 15=5.08V
// in 0.08V steps. 0x02 = 4.04V (a protective floor still above the AXP2101's run
// minimum, but below the sagging PwrCAN rail).
static constexpr uint8_t VINDPM_OPT = 0x02;  // 4.04V
// Reg 0x16 low 3 bits VBUS input current limit: 0=100, 1=500, 2=900mA...
static constexpr uint8_t IINLIM_OPT = 0x01;  // 500mA

inline float vindpmVolts(uint8_t reg) { return 3.88f + 0.08f * (reg & 0x0F); }

// Rough single-cell LiPo open-circuit-voltage -> state-of-charge. The AXP2101's
// coulomb gauge (reg 0xA4) reads 0 unless it has run a full learning cycle (and
// a hot-inserted reserve cell never converges), so we estimate SOC from terminal
// voltage instead. Good to a few % at rest; reads a bit low under heavy load.
inline int lipoSoc(float v) {
  static const struct { float v; int p; } c[] = {
    {3.30f, 0}, {3.50f, 8},  {3.60f, 15}, {3.70f, 25}, {3.75f, 35}, {3.80f, 45},
    {3.85f, 55}, {3.90f, 63}, {3.95f, 70}, {4.00f, 76}, {4.05f, 82}, {4.10f, 88},
    {4.15f, 94}, {4.20f, 100},
  };
  const int n = sizeof(c) / sizeof(c[0]);
  if (v <= c[0].v)   return 0;
  if (v >= c[n-1].v) return 100;
  for (int i = 1; i < n; i++) {
    if (v < c[i].v) {
      float f = (v - c[i-1].v) / (c[i].v - c[i-1].v);
      return c[i-1].p + (int)(f * (c[i].p - c[i-1].p) + 0.5f);
    }
  }
  return 100;
}

struct Status {
  bool  onBoat;   // VBUS present == boat power live
  bool  hasBatt;  // a cell is installed (Vbat > 3.0V)
  int   level;    // SOC %, or -1 when no battery
  float vbat;     // battery terminal voltage, V
  float vbus;     // bus/VBUS input voltage, V (0 when no boat power)
  int   chg;      // -1 discharging / 0 standby / 1 charging
  float tdie;     // AXP2101 die temperature, deg C
  bool  tdieOk;   // tdie within a plausible range
};

inline Status read() {
  auto& axp = M5.Power.Axp2101;
  Status s;
  s.onBoat  = axp.isVBUS();                 // VBUS present == boat power
  s.vbat    = axp.getBatteryVoltage();      // V (reg 0x34)
  s.hasBatt = s.vbat > 3.0f;                // below this == no cell present
  const int gauge = M5.Power.getBatteryLevel();  // AXP2101 gauge (often 0)
  // Prefer the coulomb gauge only if it returns something plausible; else estimate.
  s.level   = !s.hasBatt ? -1
            : (gauge >= 1 && gauge <= 100) ? gauge
            : lipoSoc(s.vbat);
  s.vbus    = axp.getVBUSVoltage();         // V (reg 0x38, 0 if no VBUS)
  s.chg     = axp.getChargeStatus();        // -1 discharge / 0 standby / 1 charge
  s.tdie    = axp.getInternalTemperature(); // deg C (reg 0x3C)
  s.tdieOk  = (s.tdie > -20.0f && s.tdie < 120.0f);
  return s;
}

inline const char* chgString(int chg) {
  return (chg > 0) ? "chg" : (chg < 0) ? "dis" : "std";
}

// Configure the AXP2101 so boat power (PwrCAN) is primary with automatic battery
// failover, and so the (optional) Bottom3 can charge off the sagging bus rail.
// Call once in setup(), after Display::setup() so the logging works. Safe with
// or without a battery installed.
inline void setup() {
  // CRITICAL for the PwrCAN stack: when a battery is present, M5.begin() leaves
  // the CoreS3 DRIVING 5V out onto the M-Bus (boost, to power downstream
  // modules). That ~5.1V back-biases the PwrCAN's 4.9V bus feed out of the bus,
  // so with the battery installed the stack silently runs off battery and
  // ignores boat power (VBUS reads absent). Put the bus in INPUT mode so the
  // PwrCAN powers the stack and the AXP2101 sees VBUS (and can charge). Failover
  // is unaffected: boat-power loss -> no bus 5V -> AXP runs off battery.
  M5.Power.setExtOutput(false);

  // Enable the AXP2101 internal die-temperature ADC channel (0x30 bit 4);
  // M5Unified's init leaves it off, so reg 0x3C otherwise reads garbage.
  M5.Power.Axp2101.bitOn(0x30, 1 << 4);

  // Cap charge current so battery charging can't overrun the PwrCAN's 2W
  // (~400mA @ 5V) isolated supply and brown out the logger.
  M5.Power.Axp2101.setChargeCurrent(CHARGE_MA);

  // Lower VINDPM + set input current limit so the AXP2101 will charge from the
  // PwrCAN's sagging bus rail (it otherwise only charges off a stiff USB 5V).
  // Read-modify-write the low bits; log before/after to prove the default
  // VINDPM was the block.
  auto& axp = M5.Power.Axp2101;
  uint8_t v0 = axp.readRegister8(0x15);
  uint8_t i0 = axp.readRegister8(0x16);
  axp.writeRegister8(0x15, (v0 & 0xF0) | VINDPM_OPT);
  axp.writeRegister8(0x16, (i0 & 0xF8) | IINLIM_OPT);
  uint8_t v1 = axp.readRegister8(0x15);
  LOGF_INFO("power::setup VINDPM 0x15 %02X(%.2fV)->%02X(%.2fV) IINLIM 0x16 %02X->%02X chg=%umA",
            v0, vindpmVolts(v0), v1, vindpmVolts(v1), i0,
            axp.readRegister8(0x16), (unsigned)CHARGE_MA);
}

// Edge-log boat-power loss/restore. Call each display refresh; logs only on the
// transition. For a logger, a boat-power-loss event in the trace is the marker
// that the session is now running on reserve and may end soon.
inline void logSourceChange(const Status& s) {
  static bool lastOnBoat = true;
  static bool haveLast   = false;
  if (!haveLast || s.onBoat != lastOnBoat) {
    if (s.onBoat)
      LOGF_WARN("*** POWER -> BOAT (VBUS restored, Vbat=%.3fV level=%d%%)", s.vbat, s.level);
    else
      LOGF_WARN("*** POWER -> BATTERY (boat power LOST, Vbat=%.3fV level=%d%%)", s.vbat, s.level);
    lastOnBoat = s.onBoat;
    haveLast   = true;
  }
}

// --- Graceful shutdown on sustained battery operation ----------------------
//
// Boat power (PwrCAN) is the reliable source; a long continuous run on the
// reserve battery means the boat has been powered down or the logger was
// unplugged. After BATTERY_SHUTDOWN_MS continuously on battery, the application
// should close its logs and call shutdown() to power the AXP2101 off and
// preserve the cell. Re-applying boat power (VBUS) boots the stack again.
static constexpr uint32_t BATTERY_SHUTDOWN_MS = 10UL * 60UL * 1000UL;  // 10 min

// Milliseconds spent *continuously* on battery (0 while on boat power). Resets
// the instant boat power returns, so brief PwrCAN dropouts don't accumulate.
// Drive it by calling once per loop; uses millis() deltas, so it is wrap-safe.
inline uint32_t onBatteryElapsedMs(const Status& s) {
  static bool counting   = false;
  static uint32_t startMs = 0;
  if (s.onBoat) { counting = false; return 0; }
  uint32_t now = millis();
  if (!counting) { counting = true; startMs = now; }
  return (uint32_t)(now - startMs);
}

// Power the device off via the AXP2101 (lowest-power state; preserves the
// battery). Does NOT return. The caller must flush/close logs first.
inline void shutdown() {
  M5.Power.powerOff();
}

}  // namespace power

#endif  // CORES3
