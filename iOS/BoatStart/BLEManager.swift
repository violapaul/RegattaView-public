

// TODO:
//  - serious error handling and restart
//  - shutdown cleanly.
//  - log errors in a better way than print??

import Foundation
import CoreBluetooth

/// Class manages the interface with Bluetooth Low Energy.
class BLEManager: ChangedUpdate, CBCentralManagerDelegate, CBPeripheralDelegate {
    private var centralManager: CBCentralManager!
    private var connectedPeripheral: CBPeripheral!
    
    private var peripherals = [CBPeripheral]()
    private var currentPeripheral: CBPeripheral?
    private var peripheralMatcher: Regex<Substring>
    private var uuid: CBUUID
    
    var messageCount = 0
    
    var isReceivingMessages: Bool = false
    
    var fastConnect = false

    var currentData: Data?

    /// Connects to BLE peripherals
    /// Call with a regex that matches the peripheral name, and the characteristic name.
    init(peripheralRegex: Regex<Substring>, characteristcName: String, fastConnect: Bool = false) {
        self.peripheralMatcher = peripheralRegex
        self.uuid = CBUUID(string: characteristcName)
        self.fastConnect = fastConnect

        super.init()
        centralManager = CBCentralManager(delegate: self, queue: nil)
    }
    
    /// Search among the current peripherals to find first matching peripheral.  If it exists it grabs it, stops scanning, and connects.
    public func findMatchingPeripheral() {
        for peripheral in peripherals {
            if let name = peripheral.name {
                if name.firstMatch(of: peripheralMatcher) != nil {
                    print("Found and matched \(name)")
                    currentPeripheral = peripheral
                    stopScan()
                    if fastConnect {
                        connect()
                    }
                    return
                }
            }
        }
        return
    }
    
    public func hasMatchingPeripheral() -> Bool {
        return currentPeripheral != nil
    }
    
    public func connect() {
        if hasMatchingPeripheral() {
            connect(peripheral: currentPeripheral!)
        } else {
            print(#function, "Error cannot connect, peripheral is missing")
        }
    }
    
    func startScan() {
        centralManager.scanForPeripherals(withServices: nil, options: nil)
    }
    
    func stopScan() {
        centralManager.stopScan()
    }

    func connect(peripheral: CBPeripheral) {
        centralManager.connect(peripheral, options: nil)
     }
    
    /// First step is to discover the services, and then the characteristics.
    func discoverCharacteristics(peripheral: CBPeripheral) {
        guard let services = peripheral.services else { return }
        for service in services {
            peripheral.discoverCharacteristics(nil, for: service)
        }
    }
    
    // Callbacks follow.
    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        switch central.state {
            case .poweredOn:
                /// once all is well start scanning
                startScan()
            case .poweredOff:
                // Alert user to turn on Bluetooth
                print(#function, "Turn on Bluetooth")
            case .resetting:
                // Wait for next state update and consider logging interruption of Bluetooth service
                print(#function, "Resetting")
            case .unauthorized:
                // Alert user to enable Bluetooth permission in app Settings
                print(#function, "Unauthorized")
            case .unsupported:
                // Alert user their device does not support Bluetooth and app will not work as expected
                print(#function, "Unsupported")
            case .unknown:
               // Wait for next state update
               print(#function, "unknown state")
            @unknown default:
                print(#function, "Should fail here.")
        }
    }

    
    /// Callback for peripheral discovery during scan.  Add the unique peripherals.
    func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral, advertisementData: [String : Any], rssi RSSI: NSNumber) {
        if(!peripherals.contains(peripheral)) {
            peripherals.append(peripheral)
            print(#function, "found a device: " + (peripheral.name ?? "device with no name"))
            self.findMatchingPeripheral()
        }
    }
     
    func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        // Handle error
    }

    /// Once you connect, discover services.
    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        print(#function, " peripheral connected \(peripheral.name ?? "no name")")
        self.connectedPeripheral = peripheral
        peripheral.delegate = self
        peripheral.discoverServices(nil)
    }

    /// Once you have discovered services, they each have a charateristic (which is more like a channel)
    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        guard let services = peripheral.services else { return }
        for service in services {
            print(#function, "\(service)")
            print(#function, "\(service.uuid)")
        }
        discoverCharacteristics(peripheral: peripheral)
    }

    /// Discover a characterist
    func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        guard let characteristics = service.characteristics else { return }
        for characteristic in characteristics {
            print(#function, "\(characteristic)")
            if characteristic.uuid == self.uuid {
                print("  *****  transmit characteristic!!")
                connectedPeripheral.setNotifyValue(true, for: characteristic)
                print(#function, "\(characteristic)")
            }
        }
    }
    
    func peripheral( _ peripheral: CBPeripheral,
        didUpdateNotificationStateFor characteristic: CBCharacteristic,
        error: Error?
    ) {
        print(#function, "Received an error: ", error as Any)
    }

    /// this is the major callback for data
    func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        if let error = error {
            print(#function, "\(error)")
            return
        }
        isReceivingMessages = true
        currentData = characteristic.value
        messageCount += 1
        didChange()
    }
    
}
