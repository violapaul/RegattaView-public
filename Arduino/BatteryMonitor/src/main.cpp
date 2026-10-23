// Power-source / battery monitor for a boat stack:
//   bottom : M5GO Bottom3   (LiPo battery -> AXP2101 BAT)
//   middle : PwrCAN 13.2     (9-24V boat power -> isolated 5V onto the M5-Bus)
//   top    : M5Stack CoreS3  (ESP32-S3 + AXP2101 PMIC)
//
// Power failover is AUTOMATIC in the AXP2101 power path: when the PwrCAN feeds
// 5V on the bus the system runs off boat power and trickle-charges the Bottom3;
// when boat power drops the AXP2101 switches to the Bottom3 seamlessly. No
// firmware switching is involved. This sketch just *reports* which source is
// live (so a boat-power loss is visible/loggable) and caps charge current so
// charging never overruns the PwrCAN's isolated supply.
//
// Hardware facts that shape this code:
//  - PwrCAN isolation = F0505S-2WR3 = 2W => only ~400mA @ 5V to the whole stack.
//    So charge current is capped (CHARGE_MA) to leave headroom for run current.
//  - The PwrCAN bus 5V registers as VBUS on the AXP2101 (verified on-bench), so
//    isVBUS() == "boat power present". VBUS gone => running on the Bottom3.
//  - The AXP2101 has NO battery-current ADC and the CoreS3 has no shunt, so
//    charge/discharge current in mA is simply not measurable here (M5Unified's
//    getBatteryChargeCurrent/DischargeCurrent/Power all return 0). We show
//    direction (charge/standby/discharge) from the charge-status register only.

#include <M5Unified.h>

// Cap battery charge current to protect the PwrCAN 2W (~400mA) isolated supply.
// 125mA is the lowest AXP2101 step; raise if you have a beefier 5V feed.
static constexpr uint16_t CHARGE_MA = 125;

// The PwrCAN's isolated 5V sags to ~4.3V under load -- below the AXP2101's
// default VBUS input-voltage limit (VINDPM), so the charger refuses to charge
// on boat power even though it charges fine off a stiff 5V USB. Lower VINDPM so
// the weaker bus rail is accepted as a charge source. Reg 0x15 low nibble:
// 0=3.88V .. 15=5.08V in 0.08V steps. 0x02 = 4.04V (a protective floor still
// above the AXP2101's run minimum, but below the 4.31V PwrCAN rail).
static constexpr uint8_t VINDPM_OPT = 0x02;            // 4.04V
// Reg 0x16 low 3 bits VBUS input current limit: 0=100,1=500,2=900mA...
static constexpr uint8_t IINLIM_OPT = 0x01;            // 500mA
static float vindpmVolts(uint8_t reg) { return 3.88f + 0.08f * (reg & 0x0F); }

static bool s_lastOnBoat = true;   // for edge-logging source transitions
static bool s_haveLast   = false;

// Rough single-cell LiPo open-circuit-voltage -> state-of-charge. The AXP2101's
// coulomb-gauge (reg 0xA4) reads 0 unless it has run a full learning cycle, so
// for a reserve battery we estimate SOC from terminal voltage instead. Good to
// a few % at rest; sags under load (reads a bit low while discharging hard).
static int lipoSoc(float v) {
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

static void drawScreen() {
  auto& axp = M5.Power.Axp2101;

  const bool  onBoat  = axp.isVBUS();                    // VBUS present == boat power
  const int   gauge   = M5.Power.getBatteryLevel();      // AXP2101 reg 0xA4 (often 0)
  const float vbat    = axp.getBatteryVoltage();         // V (reg 0x34)
  const bool  hasBatt = vbat > 3.0f;                     // below this == no cell present
  // Prefer the coulomb gauge only if it returns something plausible; else estimate.
  const int   level   = !hasBatt ? -1
                      : (gauge >= 1 && gauge <= 100) ? gauge
                      : lipoSoc(vbat);
  const float vbus    = axp.getVBUSVoltage();            // V (reg 0x38, 0 if no VBUS)
  const int   chg     = axp.getChargeStatus();           // -1 discharge / 0 standby / 1 charge
  const float tdie    = axp.getInternalTemperature();    // deg C (reg 0x3C)
  const bool  tdieOk  = (tdie > -20.0f && tdie < 120.0f);

  // Edge-log source transitions to serial (boat-power loss / restore event).
  if (!s_haveLast || onBoat != s_lastOnBoat) {
    Serial.printf("*** SOURCE %s  (Vbat=%.3fV level=%d%%)\n",
                  onBoat ? "-> BOAT POWER (VBUS up)" : "-> BATTERY (boat power LOST)",
                  vbat, level);
    s_lastOnBoat = onBoat;
    s_haveLast   = true;
  }

  const char* chgState =
      (chg > 0) ? "charging" : (chg < 0) ? "discharging" : "standby";

  uint16_t barColor = GREEN;
  if (level >= 0 && level < 20)      barColor = RED;
  else if (level >= 0 && level < 50) barColor = ORANGE;

  M5.Display.startWrite();
  M5.Display.fillScreen(BLACK);
  M5.Display.setTextDatum(top_left);

  // Title.
  M5.Display.setTextColor(WHITE, BLACK);
  M5.Display.setTextSize(2);
  M5.Display.setCursor(8, 6);
  M5.Display.print("Boat Stack Power");

  // SOURCE banner -- the headline.
  M5.Display.setTextSize(3);
  if (onBoat) { M5.Display.setTextColor(GREEN, BLACK);  M5.Display.setCursor(8, 32); M5.Display.print("SRC: BOAT"); }
  else        { M5.Display.setTextColor(RED,   BLACK);  M5.Display.setCursor(8, 32); M5.Display.print("SRC: BATTERY"); }

  // Battery % + bar.
  M5.Display.setTextColor(WHITE, BLACK);
  M5.Display.setTextSize(3);
  M5.Display.setCursor(8, 70);
  if (level >= 0) M5.Display.printf("%d%%", level);
  else            M5.Display.print("NO BAT");

  const int bx = 100, by = 72, bw = 212, bh = 22;
  M5.Display.drawRect(bx, by, bw, bh, WHITE);
  if (level >= 0) {
    int fill = (bw - 4) * level / 100;
    M5.Display.fillRect(bx + 2, by + 2, fill, bh - 4, barColor);
  }

  // Details.
  M5.Display.setTextSize(2);
  M5.Display.setTextColor(WHITE, BLACK);
  M5.Display.setCursor(8, 108);
  M5.Display.printf("Vbat  %.3f V  (%s)", vbat, chgState);
  M5.Display.setCursor(8, 132);
  if (onBoat) {
    // Warn (orange) if the isolated 5V is sagging -- a sign the 2W PwrCAN
    // supply is loaded near its limit (worse with no battery to buffer).
    M5.Display.setTextColor(vbus < 4.6f ? ORANGE : WHITE, BLACK);
    M5.Display.printf("Boat  %.2f V in", vbus);
    M5.Display.setTextColor(WHITE, BLACK);
  } else {
    M5.Display.print("Boat  -- (no VBUS)");
  }
  M5.Display.setCursor(8, 156);
  M5.Display.printf("Chg   %umA  Vlim %.2f", (unsigned)CHARGE_MA,
                    vindpmVolts(axp.readRegister8(0x15)));
  M5.Display.setCursor(8, 180);
  if (tdieOk) M5.Display.printf("Temp  %.1f C", tdie);
  else        M5.Display.print("Temp  n/a");

  M5.Display.endWrite();

  Serial.printf("src=%s  level=%d%%  Vbat=%.3fV  Vbus=%.2fV  %s  Tdie=%s\n",
                onBoat ? "BOAT" : "BATT", level, vbat, vbus, chgState,
                tdieOk ? String(tdie, 1).c_str() : "n/a");
}

void setup() {
  auto cfg = M5.config();
  M5.begin(cfg);
  M5.Display.setRotation(1);
  Serial.begin(115200);

  // CRITICAL for the PwrCAN stack: when a battery is present, M5.begin() leaves
  // the CoreS3 DRIVING 5V out onto the M-Bus (boost, to power downstream
  // modules). That ~5.1V back-biases the PwrCAN's 4.9V bus feed out of the bus,
  // so with the battery installed the stack silently runs off battery and
  // ignores boat power (VBUS reads absent). Put the bus in INPUT mode so the
  // PwrCAN powers the stack and the AXP2101 sees VBUS (and can charge).
  // Failover is unaffected: boat power loss -> no bus 5V -> AXP runs off battery.
  M5.Power.setExtOutput(false);

  // Enable the AXP2101 internal die-temperature ADC channel (0x30 bit 4);
  // M5Unified's init leaves it off, so reg 0x3C otherwise reads garbage.
  M5.Power.Axp2101.bitOn(0x30, 1 << 4);

  // Cap charge current so battery charging can't overrun the PwrCAN's 2W
  // (~400mA @ 5V) isolated supply and brown out the stack.
  M5.Power.Axp2101.setChargeCurrent(CHARGE_MA);

  // Lower VINDPM + set input current limit so the AXP2101 will charge from the
  // PwrCAN's ~4.3V bus rail (it otherwise only charges off a stiff USB 5V).
  // Read-modify-write the low bits; log the before/after to prove the default
  // VINDPM was the block.
  {
    auto& axp = M5.Power.Axp2101;
    uint8_t v0 = axp.readRegister8(0x15);
    uint8_t i0 = axp.readRegister8(0x16);
    axp.writeRegister8(0x15, (v0 & 0xF0) | VINDPM_OPT);
    axp.writeRegister8(0x16, (i0 & 0xF8) | IINLIM_OPT);
    uint8_t v1 = axp.readRegister8(0x15);
    uint8_t i1 = axp.readRegister8(0x16);
    Serial.printf("VINDPM 0x15: %02X (%.2fV) -> %02X (%.2fV) ; IINLIM 0x16: %02X -> %02X\n",
                  v0, vindpmVolts(v0), v1, vindpmVolts(v1), i0, i1);
  }

  drawScreen();
}

void loop() {
  M5.update();
  static uint32_t last = 0;
  if (millis() - last >= 2000) {  // refresh every 2 s
    last = millis();
    drawScreen();
  }
}
