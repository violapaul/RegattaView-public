//
//  RaceBox.swift
//  Basic Chat MVC
//
//  Created by Paul Viola on 11/6/22.
//

import Foundation

/// Manages the interface the RaceBox and deserializing the responses.  Uses the BLE interface.
class RaceBoxManager: ChangedUpdate {

    private var tic = Date.now
    
    /// Status information about the current connected device
    var latency = 0.0
    var messageCount = 0
    var errorCount = 0
    var status = ""
    
    var receivedPacket = Data()
    var responseIsGood  = false
    var currentResponse = Response()

    var bleManager: BLEManager
    
    struct Response : Encodable {
//        var ITOW = 0
//        var year = 0
//        var month = 0
//        var day = 0
//        var hour = 0
//        var minute = 0
//        var second = 0
//        var validity = 0
//        var timeAccuracy = 0
//        var nanoseconds = 0
        
        var lon = 0.0
        var lat = 0.0
        var sog = 0.0
        var cog = 0.0
//        var wgsAltitude = 0.0
//        var mslAltitude = 0.0
        var hAccuracy = 0.0
        var vAccuracy = 0.0
        var sogAccuracy = 0.0
        var cogAccuracy = 0.0
        
        var forceX = 0.0
        var forceY = 0.0
        var forceZ = 0.0
//
//        var rateX = 0.0
//        var rateY = 0.0
//        var rateZ = 0.0
    }


    
    override init () {
        let deviceName = /racebox mini/.ignoresCase()
        let characteristicName = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"
        bleManager = BLEManager(peripheralRegex: deviceName, characteristcName: characteristicName, fastConnect: true)

        super.init()
 
        bleManager.addOnChanged(dataUpdated) // Callback when the data is updated.
    }
    
    func hasRaceBox() -> Bool {
        return bleManager.hasMatchingPeripheral()
    }
    
    func connect() {
        if hasRaceBox() {
            bleManager.connect()
        } else {
            print(#function, "Racebox is missing, cannot connect.")
        }
    }
    
    
    /// Callback when data is available from the BLE device.
    func dataUpdated() {
        receivedPacket = bleManager.currentData!  // Since this is a callback, the currentData should be good
        responseIsGood = checkPacketIsGood()
        if responseIsGood {
            extractResponseFromPacket()
        }
        updateStats()
        updateStatus()
        didChange()
    }

    /// Update some stats on packets.  Counts, errorcounts, average latency.
    func updateStats () {
        messageCount += 1
        if !responseIsGood {
            errorCount += 1
            print(#function, "Packet is not good headerIsGood \(headerIsGood()) checksumIsGood \(checksumIsGood())")
        }
        /// Exponential average of latency between packets.
        let toc = Date.now
        let delta = toc.timeIntervalSince(tic)
        latency = 0.99 * latency + 0.01 * delta
        tic = toc
    }
    
    func updateStatus() {
        var res = String(format: "%0.3f", latency)
        res += String(format: ", %05d", messageCount)
        res += String(format: ", %05d", bleManager.messageCount)
        res += String(format: ", %05d", errorCount)
        self.status = res
    }

    /// Convenience function for pulling data from the response.  Offset is the byte offset in the racebox doc.  Type is the
    public func get<T>(_ offset: Int, _ type: T.Type) -> T where T: ExpressibleByIntegerLiteral {
        // Skip over the header
        let realOffset = offset + 6
        let value = self.receivedPacket.dropFirst(realOffset).to(type: type)
        if value != nil {
            return value!
        } else {
            return 0
        }
    }
    
    public func extractResponseFromPacket() {
        currentResponse = Response(
            lon: Double(get(24, Int32.self))/10.0e6,
            lat: Double(get(28, Int32.self))/10.0e6,
            sog: Double(get(48, Int32.self))/1000.0,  // millimeters per second so divide by 1000
            cog: Double(get(52, Int32.self))/10.0e4,  // Doc says 10^5, but it seems wrong.


            hAccuracy:  Double(get(40, UInt32.self))/1000.0,
            vAccuracy:  Double(get(44, UInt32.self))/1000.0,
            sogAccuracy:  Double(get(56, UInt32.self))/1000.0,
            cogAccuracy:  Double(get(60, UInt32.self))/10.0e5,
            
            forceX: Double(get(68, Int16.self))/1000.0,
            forceY: Double(get(70, Int16.self))/1000.0,
            forceZ: Double(get(72, Int16.self))/1000.0
        )
    }
    
    func checkPacketIsGood() -> Bool {
        return headerIsGood() && checksumIsGood()
    }
    
    public func checksumIsGood() -> Bool {
        var CK_A: UInt8 = 0
        var CK_B: UInt8 = 0
        let len = receivedPacket.count
        let dataToCheck = receivedPacket.dropFirst(2).prefix(upTo: len-2)

        for byte in  dataToCheck {
            CK_A = CK_A &+ byte
            CK_B = CK_B &+ CK_A
        }
        let isGood = receivedPacket[len-2] == CK_A && receivedPacket[len-1] == CK_B
        if isGood {
            return true
        } else {
            print(#function, "Checksum error")
            return false
        }
    }
    
    /// Check that the bytes in data are equal to the bytes in the array
    func bytesEqual(data: Data, bytes: [UInt8]) -> Bool
    {
        if data.count < bytes.count {
            return false
        }
        for (b1, b2) in zip(data, bytes) {
            if (b1 != b2) {
                return false
            }
        }
        return true
    }
    
    public func headerIsGood() -> Bool {
        let headerBytes: [UInt8] = [0xB5, 0x62, 0xFF, 0x01, 0x50, 0x00]
        if !bytesEqual(data: receivedPacket, bytes: headerBytes) {
            print(#function, "  ERROR bytes are not equal")
            print(receivedPacket.prefix(6) as NSData)
            return false
        }
        return true
    }
    
    
    
}
