//
//  WiFiHelper.swift
//  BoatData
//

import Foundation
import NetworkExtension
import CoreLocation
import Security

class WiFiHelper: NSObject, ObservableObject, CLLocationManagerDelegate {
    static let shared = WiFiHelper()

    static let TARGET_SSID = "BoatBrain"
    private let passwordService = "RegattaView.BoatBrain.WiFi"

    private var savedPassword: String? {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: passwordService, kSecAttrAccount as String: Self.TARGET_SSID,
            kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    func savePassword(_ password: String) -> Bool {
        guard (16...63).contains(password.utf8.count) else { return false }
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: passwordService, kSecAttrAccount as String: Self.TARGET_SSID]
        let attributes: [String: Any] = [kSecValueData as String: Data(password.utf8),
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        var status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            status = SecItemAdd(query.merging(attributes) { _, new in new } as CFDictionary, nil)
        }
        if status == errSecSuccess { reconnectToBoatBrain() }
        return status == errSecSuccess
    }

    @Published var ssid: String = "?"
    @Published var locationAuthorized: Bool = false

    private let locationManager = CLLocationManager()
    private var reconnectTimer: Timer?

    override init() {
        super.init()
        locationManager.delegate = self
        checkAuthorization()
        startMonitoring()
    }

    func requestPermission() {
        locationManager.requestWhenInUseAuthorization()
    }

    func checkAuthorization() {
        let status = locationManager.authorizationStatus
        locationAuthorized = (status == .authorizedWhenInUse || status == .authorizedAlways)
        logInfo("Location auth status: \(status.rawValue), authorized: \(locationAuthorized)")
        if locationAuthorized {
            updateSSID()
        }
    }

    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        checkAuthorization()
    }

    func startMonitoring() {
        // Check WiFi status every 5 seconds
        reconnectTimer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            self?.checkAndReconnect()
        }
    }

    func checkAndReconnect() {
        guard locationAuthorized else { return }

        NEHotspotNetwork.fetchCurrent { [weak self] network in
            DispatchQueue.main.async {
                if let network = network {
                    self?.ssid = network.ssid
                    // Connected to wrong network - try to switch to BoatBrain
                    if network.ssid != WiFiHelper.TARGET_SSID {
                        self?.reconnectToBoatBrain()
                    }
                } else {
                    self?.ssid = "No WiFi"
                    // Not connected to any WiFi - try to reconnect to BoatBrain
                    self?.reconnectToBoatBrain()
                }
            }
        }
    }

    func reconnectToBoatBrain() {
        guard let password = savedPassword else { return }
        logInfo("Attempting to reconnect to \(WiFiHelper.TARGET_SSID)")
        ConnectionHistory.shared.log(.reconnecting, "Joining \(WiFiHelper.TARGET_SSID)...")

        let config = NEHotspotConfiguration(ssid: WiFiHelper.TARGET_SSID, passphrase: password, isWEP: false)
        config.joinOnce = false

        NEHotspotConfigurationManager.shared.apply(config) { error in
            DispatchQueue.main.async {
                if let nsError = error as NSError? {
                    // Error code 13 = already associated
                    if nsError.domain == NEHotspotConfigurationErrorDomain && nsError.code == 13 {
                        logInfo("Already connected to \(WiFiHelper.TARGET_SSID)")
                        self.ssid = WiFiHelper.TARGET_SSID
                    } else {
                        logWarn("Failed to join \(WiFiHelper.TARGET_SSID): \(nsError.localizedDescription)")
                        ConnectionHistory.shared.log(.error, "WiFi join failed")
                    }
                } else {
                    logInfo("Successfully joined \(WiFiHelper.TARGET_SSID)")
                    ConnectionHistory.shared.log(.connected, "Joined \(WiFiHelper.TARGET_SSID)")
                    self.ssid = WiFiHelper.TARGET_SSID
                }
            }
        }
    }

    func updateSSID() {
        guard locationAuthorized else {
            ssid = "No permission"
            return
        }

        // Use modern NEHotspotNetwork API (iOS 14+)
        NEHotspotNetwork.fetchCurrent { [weak self] network in
            DispatchQueue.main.async {
                if let network = network {
                    self?.ssid = network.ssid
                    logInfo("WiFi SSID: \(network.ssid)")
                } else {
                    self?.ssid = "No WiFi"
                    logWarn("NEHotspotNetwork.fetchCurrent returned nil")
                }
            }
        }
    }
}
