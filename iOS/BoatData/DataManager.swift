//
//  DataManager.swift
//  BoatData
//
//  Created by Paul Viola on 10/25/23.
//


import Foundation
import CoreLocation
import SwiftUI
import Spatial

// This is a helper (glue) that can be passed to SwiftUI view to edit/select a new address.  When you
// are done editing call resetAddress and it will pass it down to the data manager
class IpAddressUpdater: ObservableObject {
    @Published var ipAddress : IpAddress
    var dataManager : DataManager
    
    init() {
        ipAddress = IpAddress()
        dataManager = DataManager()
    }
    
    init(ipAddress: IpAddress, dataManager: DataManager) {
        self.ipAddress = ipAddress
        self.dataManager = dataManager
    }
    
    func resetAddress() {
        dataManager.reset(ipAddress: ipAddress)
    }
}

class IpAddress: NSObject, ObservableObject, NSCopying {
    @Published var segments = [192, 168, 4, 1]

    override init() {
    }
    
    init(ipAddress: String) {
        super.init()
        self.resetIPAddress(to: ipAddress)
    }
    
    init(segments: [Int]) {
        if segments.count != 4 {
            fatalError("Invalid IP Address")
        }
        self.segments = segments
    }
    
    func copy(with zone: NSZone? = nil) -> Any {
        let copy = IpAddress(segments: segments)
        return copy
    }
    
    func address() -> String {
        // join segments with "."
        let stringSegments = segments.map { String($0) }
        let address = stringSegments.joined(separator: ".")
        return address
    }
    
    func resetIPAddress(to: String) {
        logWarn("resetIPAddress to \(to)")
        let components = to.components(separatedBy: ".")
        guard components.count == 4 else {
            logError("Invalid IP Address: \(to) - expected 4 segments")
            return
        }
        let parsed = components.compactMap { Int($0) }
        guard parsed.count == 4 else {
            logError("Invalid IP Address: \(to) - non-numeric segments")
            return
        }
        self.segments = parsed
    }
}

/// Maintains the state of the boat.  Various sensors call in to make updates.
///  
class DataManager: NSObject, ObservableObject {
    
    // an enum for NMEA0183 vs BoatBrain
    enum Source {
        case nmea
        case boatBrain
        case boatBrainUdp
    }
    
    /// Stores GPS information
    struct InstrumentVals: Codable {
        var lat: Double = 0.0
        var lon: Double = 0.0
        
        var sog: Double = 0.0
        var cog: Double = 0.0
        
        var hdg: Double = 0.0
        var spd: Double = 0.0
        
        var aws: Double = 0.0
        var awa: Double = 0.0

        var saws: Double = 0.0
        var sawa: Double = 0.0

        var depth: Double = 0.0
        var rudder: Double = 0.0
        
        var twd: Double = 0.0   // twd from boat
        var twa: Double = 0.0   // twa from boat
        var tws: Double = 0.0   // tws from boat
        
        var magVar: Double = 15.1  // default in Seattle  BUG
        
        var ptwd: Double = 0.0   // predicted twd from boatbrain
        var ptwa: Double = 0.0  // predicted tws
        var ptws: Double = 0.0  // predicted twd
        
        var packetCount = 0
        var msgCount  = 0
        var errorCount = 0;
        var resetCount = 0
        var updateCount = 0
        var timeOfDay = ""
        
        // magnetic twd
        var mtwd: Double {
            get {
                return Mapping.compassAngle(twd - magVar)
            }
        }
        
        var mhdg: Double {
            get {
                return Mapping.compassAngle(hdg - magVar)
            }
        }
        
        var trueDepth: Double {
            get {
                return depth - 5.0
            }
        }

        init() {}
        
        //        static func empty() -> GPSPt {
        //            return GPSPt(lat: 0.0, lon: 0.0, sog: 0.0, cog: 0.0, accuracy: 0.0, isValid: false)
        //        }
    }
    
    // An array of InstrumentVals, start with a single point
    var track = [InstrumentVals()]
    var currentInstruments = InstrumentVals()
    var twdVals : [Double] = []
    var hdgVals : [Double] = []
    static let sampleRate = 10
    static let stripTime = 40
    static let stripLength = stripTime * 60 * sampleRate
    
    var dataBuffer = Data()
    
    var conn : BoatBrainConnection?
    var ipAddress : IpAddress
    var port : Int
    var source : Source
    var ipAddressUpdater : IpAddressUpdater?
    
    let timeFormatter = DateFormatter()

    var messageTerminator : Data = Data([])

    var timer: Timer?

    var msgStats = SequenceStats(alpha: 0.2, count: 100)

    // Connection state and reconnection handling
    @Published var connectionState: ConnectionState = .disconnected
    var reconnectAttempts = 0
    static let MAX_RECONNECT_DELAY = 10.0   // Max 10 seconds between attempts (was 30)
    static let BASE_RECONNECT_DELAY = 1.0   // Start with 1 second
    static let MIN_RECONNECT_INTERVAL = 2.0 // Minimum time between reconnect attempts
    static let WATCHDOG_INTERVAL = 5.0      // Check connection health every 5 seconds
    static let DATA_TIMEOUT = 5.0           // Consider connection dead if no data for 5 seconds
    static let STARTUP_GRACE_PERIOD = 10.0  // Don't trigger watchdog reconnects for first 10 seconds
    var lastConnectedTime: Date?
    var connectionStartTime: Date?
    var lastReconnectAttempt: Date?
    var watchdogTimer: Timer?
    var appStartTime: Date?

    init (address: String = "192.168.4.1", port: Int = 100, source: Source = .boatBrain) {
        self.ipAddress = IpAddress(ipAddress: address)
        self.port = port
        self.source = source
        // self.ipAddressResetter = IpAddressUpdater(ipAddress: self.ipAddress, dataManager: self)
        super.init()
        
        self.ipAddressUpdater = IpAddressUpdater(ipAddress: self.ipAddress, dataManager: self)
        self.appStartTime = Date()

        setLoggingLevel(.debug)

        timeFormatter.dateFormat = "HH:mm:ss"

        UIApplication.shared.isIdleTimerDisabled = true
        UIDevice.current.isBatteryMonitoringEnabled = true
        
        hdgVals = Array(repeating: -1.0, count: DataManager.stripLength)
        twdVals = Array(repeating: -1.0, count: DataManager.stripLength)
        
        self.reset();
        self.startTask();
    }
    

    // Start the UI update timer (20Hz) and watchdog timer (every 5 seconds)
    func startTask() {
        // UI update timer - fires every 0.05 second (20Hz)
        timer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            self.instrumentUpdate()
        }

        // Watchdog timer - fires every 5 seconds to ensure connection health
        watchdogTimer = Timer.scheduledTimer(withTimeInterval: DataManager.WATCHDOG_INTERVAL, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            self.checkConnectionHealth()
        }
    }

    /// Watchdog: Check if we're receiving data. If not, reconnect.
    /// This is the ONLY place that triggers reconnection for reliability.
    func checkConnectionHealth() {
        // Don't trigger reconnects during startup grace period
        if let startTime = appStartTime {
            let timeSinceStart = Date().timeIntervalSince(startTime)
            if timeSinceStart < DataManager.STARTUP_GRACE_PERIOD {
                logInfo("Watchdog: In startup grace period (\(String(format: "%.1f", timeSinceStart))s)")
                return
            }
        }

        let staleness = msgStats.max()
        let isReceivingData = staleness < DataManager.DATA_TIMEOUT

        if isReceivingData {
            // All good - reset backoff counter when receiving data
            if reconnectAttempts > 0 {
                logInfo("Connection healthy, resetting backoff counter")
                ConnectionHistory.shared.log(.connected, "Healthy - backoff reset")
                reconnectAttempts = 0
            }
            return
        }

        // Not receiving data - need to reconnect
        logWarn("Watchdog: No data for \(String(format: "%.1f", staleness))s, state=\(connectionState.rawValue)")

        // Check minimum interval between reconnect attempts
        if let lastAttempt = lastReconnectAttempt {
            let timeSinceLastAttempt = Date().timeIntervalSince(lastAttempt)
            if timeSinceLastAttempt < DataManager.MIN_RECONNECT_INTERVAL {
                logInfo("Watchdog: Too soon since last attempt (\(String(format: "%.1f", timeSinceLastAttempt))s), waiting")
                return
            }
        }

        // Trigger reconnection
        reconnectAttempts += 1
        let delay = getReconnectDelay()
        logWarn("Watchdog: Triggering reconnect #\(reconnectAttempts), delay=\(String(format: "%.1f", delay))s")
        ConnectionHistory.shared.log(.watchdog, "No data \(String(format: "%.1f", staleness))s - reconnect #\(reconnectAttempts)")

        lastReconnectAttempt = Date()
        DispatchQueue.main.async {
            self.connectionState = .disconnected
        }

        // Schedule the actual reconnect with backoff delay
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            self?.performReconnect()
        }
    }

    /// Actually perform the reconnection
    func performReconnect() {
        // First check if we're on the right WiFi network
        let wifi = WiFiHelper.shared

        // Force a fresh SSID check
        wifi.updateSSID()

        // If we don't know the SSID yet or we're on wrong network, wait
        if wifi.ssid == "?" || wifi.ssid == "No WiFi" || wifi.ssid == "No permission" {
            logWarn("WiFi status unknown (\(wifi.ssid)), waiting...")
            ConnectionHistory.shared.log(.reconnecting, "Checking WiFi...")

            // Retry in 1 second to let WiFi check complete
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
                self?.performReconnect()
            }
            return
        }

        if wifi.ssid != WiFiHelper.TARGET_SSID {
            logWarn("Not on BoatBrain WiFi (current: \(wifi.ssid)), joining...")
            ConnectionHistory.shared.log(.reconnecting, "Joining BoatBrain WiFi...")

            // Try to join BoatBrain
            wifi.reconnectToBoatBrain()

            // Retry in 2 seconds
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) { [weak self] in
                self?.performReconnect()
            }
            return
        }

        currentInstruments.resetCount += 1
        logWarn("Performing reconnect #\(currentInstruments.resetCount)")
        ConnectionHistory.shared.log(.reconnecting, "Reconnecting... (reset #\(currentInstruments.resetCount))")

        // Close existing connection
        conn?.close()
        conn = nil

        // Create new resetter for address
        self.ipAddressUpdater = IpAddressUpdater(ipAddress: self.ipAddress, dataManager: self)

        // Connect
        connect(onReady: { [weak self] in
            logInfo("Reconnect successful")
            ConnectionHistory.shared.log(.connected, "Connected to \(self?.ipAddress.address() ?? "?")")
            self?.msgStats.reset()
        })
    }
    
    // If we haven't received a message in 5 seconds then the data is stale
    func dataStale() -> Bool {
        return msgStats.max() > 5.0
    }
    
    // return a random array of floats with mean and std
    static func randomArray(mean: Double, std: Double, count: Int) -> [Double] {
        var result = [Double]()
        let noise = std/4.0
        for n in 0..<count {
            // bad hack
            let val = Double.random(in: (mean-noise)...(mean+noise))
            let offset = 2.8*Double(n)/Double(count) - 1.0
            result.append(val + std*offset)
        }
        return result
    }

    // given two compass values (0 to 360) check to see if the the difference greater than max, if so clip to max
    func clipCompassAngle(new: Double, old: Double, maxDiff: Double) -> Double {
        let diff = Mapping.signedAngle(new - old)
        let delta = min(maxDiff, max(-maxDiff, diff))
        return Mapping.compassAngle(delta + old)
    }
    
    func appendClippedValue(track: inout [Double], new: Double, maxDiff: Double = 1.0) {
        let last = track.last!
        if last == -1 {
            track.append(new)
        } else {
            let clipped = clipCompassAngle(new: new, old: last, maxDiff: maxDiff)
            track.append(clipped)
        }
        clipToN(array: &track, n: DataManager.stripLength)
    }
    
    func instrumentUpdate() {
        objectWillChange.send()  // Send an update to the UX
        currentInstruments.updateCount += 1
        // append the currentInstruments to the track
        track.append(currentInstruments)
        // Boat brain
        appendClippedValue(track: &twdVals, new: currentInstruments.mtwd, maxDiff: 5.0)
        appendClippedValue(track: &hdgVals, new: currentInstruments.mhdg, maxDiff: 5.0)
        // trim to keep only the last N items
        self.clipToN(array: &track, n: DataManager.stripLength)
        currentInstruments.timeOfDay = timeFormatter.string(from: Date())
    }
    
    /// clip the array to the last N items
    /// - Parameters:
    ///   - array: generic array
    ///   - n: number to remore
    func clipToN<T>(array: inout [T], n: Int) {
        if array.count > n {
            array.removeFirst(array.count - n)
        }
    }
    
    /// Establish a connection to the server, and handle messages
    func connect(onReady: @escaping () -> Void) {
        logWarn("Calling connect to \(ipAddress.address()):\(port)")
        connectionStartTime = Date()

        // case for source types
        if let conn = self.conn {
            conn.close()
        }

        // Wrapper to handle successful connection
        let onReadyWrapper: () -> Void = { [weak self] in
            guard let self = self else { return }
            self.reconnectAttempts = 0  // Reset backoff on successful connection
            self.lastConnectedTime = Date()
            logInfo("Connection established, reset reconnect attempts")
            onReady()
        }

        // Error handler - just log, watchdog will handle reconnection
        let onErrorHandler: () -> Void = { [weak self] in
            logWarn("Connection error occurred")
            ConnectionHistory.shared.log(.error, "Connection error")
            DispatchQueue.main.async {
                self?.connectionState = .failed
            }
            // Don't try to reconnect here - let the watchdog handle it
        }

        var newConn: BoatBrainConnection?

        switch self.source {
        case .nmea:
            self.messageTerminator = Data("\r\n".utf8)
            newConn = NetworkConnection(address: ipAddress.address(), port: port, maxLength: 1000,
                                         onData: self.handleNmea, onError: onErrorHandler, onReady: onReadyWrapper)
        case .boatBrain:
            // message terminator is 0xFFFF followed by \n
            self.messageTerminator = Data([0xFF, 0xFF, 0x0A])
            newConn = NetworkConnection(address: ipAddress.address(), port: port, maxLength: 1024,
                                         onData: self.handleBoatBrain, onError: onErrorHandler, onReady: onReadyWrapper)
        case .boatBrainUdp:
            self.messageTerminator = Data([0xFF, 0xFF, 0x0A])
            newConn = UdpConnection(address: ipAddress.address(), port: port, onData: self.handleBoatBrain, onError: onErrorHandler, onReady: onReadyWrapper)
        }

        guard let conn = newConn else {
            logError("Failed to create connection")
            DispatchQueue.main.async {
                self.connectionState = .failed
            }
            // Don't try to reconnect here - let the watchdog handle it
            return
        }

        self.conn = conn

        // Set up state change callback
        conn.onStateChange = { [weak self] state in
            DispatchQueue.main.async {
                self?.connectionState = state
            }
        }
    }

    /// Calculate reconnect delay with exponential backoff
    func getReconnectDelay() -> Double {
        let delay = DataManager.BASE_RECONNECT_DELAY * pow(1.5, Double(reconnectAttempts))
        return min(delay, DataManager.MAX_RECONNECT_DELAY)
    }

    /// Called when user changes IP address
    func reset(ipAddress: IpAddress) {
        logWarn("Resetting connection to \(ipAddress.address())")
        self.ipAddress = ipAddress
        self.reset()
    }

    /// Manual reset - used for initial connection and IP address changes
    func reset() {
        logInfo("Manual reset requested")
        reconnectAttempts = 0
        lastReconnectAttempt = nil
        performReconnect()
    }
        
    /// Find the first instance of a sequence of bytes
    /// - Returns: range of the data, or empty
    func findSequence(data: Data, sequence: Data) -> Range<Data.Index> {
        var result = 0..<0
        if let range = data.range(of: sequence) {
            result = range
        }
        return result
    }
    
    /// Called when new data is recieved, buffers the data, and if there is a complete message sends it off the callback
    /// - Parameters:
    ///   - data: raw data received
    ///   - sequence: message termination sequence
    ///   - callback: message callback
    func handleData(data: Data, sequence: Data, msgCallback : @escaping (Data) -> Void) {
        DispatchQueue.main.async {
            self.currentInstruments.packetCount += 1
            // append to dataBuffer
            self.dataBuffer.append(data)
            // Read lines, terminated by \r\n, need to remove both!
            // Carriage return is 13, line feed is 10
            var range = self.findSequence(data: self.dataBuffer, sequence: sequence)
            while (!range.isEmpty) {
                // pull off the line
                let line = self.dataBuffer.prefix(upTo: range.lowerBound)
                // remove the line from the buffer
                self.dataBuffer.removeSubrange(0..<range.upperBound)
                msgCallback(line)
                range = self.findSequence(data: self.dataBuffer, sequence: sequence)
            }
        }
    }
    
    // Call handData to extract the NMEA messages
    func handleNmea(data: Data) {
        handleData(data: data, sequence: self.messageTerminator, msgCallback: handleNmeaMessage)
    }
    
    func handleBoatBrain(data: Data) {
        handleData(data: data, sequence: self.messageTerminator, msgCallback: handleBoatBrainMessage)
    }

    
    /// Dispatches on the NMEA message type
    /// - Parameter message: NMEA 0183 sentence
    func handleNmeaMessage(message: Data) {
        guard let msgString = String(data: message, encoding: .utf8) else {
            print("Error: Cannot decode message {data}")
            return
        }
        currentInstruments.msgCount += 1
        msgStats.event()
        if let message = Nmea.dispatchToParser(sentence: msgString) {
            switch message {
            case let rmc as Nmea.RMC:
                currentInstruments.lat = rmc.latitude ?? 0.0
                currentInstruments.lon = rmc.longitude ?? 0.0
                currentInstruments.sog = rmc.speedOverGround ?? 0.0
                currentInstruments.cog = rmc.trackMadeGood ?? 0.0
            case let mwv as Nmea.MWV:
                if mwv.reference == "R" {
                    currentInstruments.awa = mwv.windAngle ?? 0.0
                    currentInstruments.aws = mwv.windSpeed ?? 0.0
                } else if mwv.reference == "T" {
                    currentInstruments.twa = mwv.windAngle ?? 0.0
                    currentInstruments.tws = mwv.windSpeed ?? 0.0
                } else {
                    print("Unknown MWV reference: \(mwv.reference ?? "nil")")
                }
            case let mwd as Nmea.MWD:
                currentInstruments.twd = mwd.windDirectionTrue ?? 0.0
                // I don't want to maintaing both magnetic and true.
                // currentInstruments.mtwd = mwd.windDirectionMagnetic ?? 0.0
                currentInstruments.tws = mwd.windSpeedKnots    ?? 0.0
            case let vhw as Nmea.VHW:
                currentInstruments.spd = vhw.speedKnots ?? 0.0
                currentInstruments.hdg = vhw.headingTrue ?? 0.0
                // currentInstruments.mhdg = vhw.headingMagnetic ?? 0.0
            default:
                print("Unknown NMEA type: \(message)")
            }
        }
    }
    
    // function that converts 4 bytes into an integer, and then a float using the multiplier
    func bytes4ToFloat(data: Data, index: Int, multiplier: Double) -> (Double, Int) {
        let subdata = data.subdata(in: index..<index+4)
        let value = subdata.withUnsafeBytes { $0.load(as: Int32.self) }
        return (Double(value) * multiplier, index+4)
    }
    
    // function that converts 2 bytes into an integer, and then a float using the multiplier
    func bytes2ToFloat(data: Data, index: Int, multiplier: Double) -> (Double, Int) {
        let subdata = data.subdata(in: index..<index+2)
        let value = subdata.withUnsafeBytes { $0.load(as: Int16.self) }
        return (Double(value) * multiplier, index+2)
    }
    
    // function that converts 4 bytes into an unsigned integer, and then a float using the multiplier
    func ubytes4ToFloat(data: Data, index: Int, multiplier: Double) -> (Double, Int) {
        let subdata = data.subdata(in: index..<index+4)
        let value = subdata.withUnsafeBytes { $0.load(as: UInt32.self) }
        return (Double(value) * multiplier, index+4)
    }
    
    // function that converts 2 bytes into an unsigned integer, and then a float using the multiplier
    func ubytes2ToFloat(data: Data, index: Int, multiplier: Double) -> (Double, Int) {
        let subdata = data.subdata(in: index..<index+2)
        let value = subdata.withUnsafeBytes { $0.load(as: UInt16.self) }
        return (Double(value) * multiplier, index+2)
    }
    
    // compute a checksum for a data message
    func checksum(data: Data) -> UInt8 {
        var sum: UInt8 = 0
        for byte in data {
            sum ^= byte
        }
        return sum
    }
    
    
    // function that deserializes a boat state from a data object
    func handleBoatBrainMessage(message: Data) {
        // Extract the header (length and checksum)
        let msgHeader = message.subdata(in: 0..<2)
        let msgData = message.subdata(in: 2..<message.count)
        let claimedSize = Int(msgHeader[0])
        guard message.count == claimedSize else {
            currentInstruments.errorCount += 1
            logWarn("Data is wrong length: \(message.count) vs \(claimedSize)")
            return
        }
        
        // Validate checksum
        let checkSum = checksum(data: msgData)
        let claimedCheckSum = msgHeader[1]
        guard checkSum == claimedCheckSum else {
            currentInstruments.errorCount += 1
            logWarn("Checksum failed: \(checkSum) vs \(claimedCheckSum)")
            return
        }
        
        var index = 0
        // Deserialize the boat state
        (currentInstruments.lat, index) = bytes4ToFloat(data: msgData, index: index, multiplier: 1e-7)
        (currentInstruments.lon, index) = bytes4ToFloat(data: msgData, index: index, multiplier: 1e-7)
        
        (currentInstruments.sog, index) = ubytes2ToFloat(data: msgData, index: index, multiplier: 1e-2)
        (currentInstruments.cog, index) = ubytes2ToFloat(data: msgData, index: index, multiplier: 1e-1)
        
        (currentInstruments.hdg, index) = ubytes2ToFloat(data: msgData, index: index, multiplier: 1e-1)
        (currentInstruments.spd, index) = ubytes2ToFloat(data: msgData, index: index, multiplier: 1e-2)
        
        (currentInstruments.aws, index) = ubytes2ToFloat(data: msgData, index: index, multiplier: 1e-2)
        (currentInstruments.awa, index) = bytes2ToFloat(data: msgData, index: index, multiplier: 1e-2)

        (currentInstruments.saws, index) = ubytes2ToFloat(data: msgData, index: index, multiplier: 1e-2)
        (currentInstruments.sawa, index) = bytes2ToFloat(data: msgData, index: index, multiplier: 1e-2)

        (currentInstruments.depth, index) = ubytes2ToFloat(data: msgData, index: index, multiplier: 1e-0)
        (currentInstruments.rudder, index) = bytes2ToFloat(data: msgData, index: index, multiplier: 1e-2)

        (currentInstruments.twd, index) = ubytes2ToFloat(data: msgData, index: index, multiplier: 1e-1)
        (currentInstruments.twa, index) = bytes2ToFloat(data: msgData, index: index, multiplier: 1e-2)
        (currentInstruments.tws, index) = ubytes2ToFloat(data: msgData, index: index, multiplier: 1e-2)

        (currentInstruments.ptwd, index) = ubytes2ToFloat(data: msgData, index: index, multiplier: 1e-1)
        (currentInstruments.ptwa, index) = bytes2ToFloat(data: msgData, index: index, multiplier: 1e-2)
        (currentInstruments.ptws, index) = ubytes2ToFloat(data: msgData, index: index, multiplier: 1e-2)

        (currentInstruments.magVar, index) = bytes2ToFloat(data: msgData, index: index, multiplier: 1e-2)
        
        objectWillChange.send()
        currentInstruments.msgCount += 1
        msgStats.event()

        // Log periodically to show data is flowing (every 100 messages ~ every 10 seconds at 10Hz)
        if currentInstruments.msgCount % 100 == 0 {
            ConnectionHistory.shared.log(.dataReceived, "Msg #\(currentInstruments.msgCount) - SOG:\(String(format: "%.1f", currentInstruments.sog))")
        }
    }
}



