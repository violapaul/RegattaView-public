
//
//  Created by Paul Viola on 11/21/22.
//

import Foundation
import CoreLocation
import SwiftUI
import Spatial

/// Maintains the state of the boat.  Various sensors call in to make updates.
///
class BoatState: NSObject, ObservableObject {
    
    // MARK: - types and structs
    typealias Angle = Double
    typealias Coord = Mapping.Coord
    typealias StartingLine = Mapping.Line
    
    /// Stores GPS information
    struct GPSPt: Codable {
        var lat: Angle
        var lon: Angle
        var sog: Double
        var cog: Double
        var accuracy: Double
        var isValid: Bool
        
        static func empty() -> GPSPt {
            return GPSPt(lat: 0.0, lon: 0.0, sog: 0.0, cog: 0.0, accuracy: 0.0, isValid: false)
        }
    }
    
    enum EventType: String, Codable {
        case none
        case phone
        case raceBox
    }
    
    /// We are going to store our tracking data using this datastructure (converted to JSON).
    struct TrkPt: Codable {
        var dateTime: Date = Date.now
        var phoneGPS: GPSPt = GPSPt.empty()
        var raceBoxGPS: GPSPt = GPSPt.empty()
        var rawHdg: Angle = 0.0
        var hdg: Angle = 0.0
        var LLA: Coord = Coord()
        var ned: Coord = Coord()
        var sog: Double = 0.0
        var cog: Double = 0.0
        var accuracy: Double = 0.0
        var boatLLA: Coord = Coord()
        var boatNED: Coord = Coord()
        var pinLLA: Coord = Coord()
        var pinNED: Coord = Coord()
        var isBoat: Bool = false
        var isPin: Bool = false
        var event = EventType.none
        var force = Coord()
        
        var timeToLine = 0.0
        var distanceToLine = 0.0
        var speedToLine = 0.0
        var batteryLevel = 0.0
        
        var tackState = IncrementalTackDetector.TackHdgState.invalid
        var afterTackHdg = 0.0
        init() {
        }
    }
    
    // MARK: - sensor management stuff
    enum GPSSensor {
        case phone
        case raceBox
        case either
    }

    // MARK: - instance vars
    private let jsonEncoder = JSONEncoder()
    
    var stateLog: [TrkPt] = []
    @Published var state = TrkPt()
    var boatOffset = Coord()
    var pinOffset = Coord()
    
    var startingLine: StartingLine?
    var activeSensor = GPSSensor.either
    let dateFormatter = DateFormatter()
    
    var speedFilter: ExponentialFilter
    var compassOffsetFilter: CompassOffsetFilter
    var tackDetector: IncrementalTackDetector
    let tackDelay = 150
    
    override init() {

        self.speedFilter = ExponentialFilter(alpha: 0.995, maxError: 0.3)
        self.compassOffsetFilter = CompassOffsetFilter(speedThreshold: 0.5, errorClip: 5.0, gain: 0.5)
        self.tackDetector = IncrementalTackDetector(tack_time: 7*25, after_tack_time: 8*25, hdg_average_time: 12*25)
        super.init()
        
        jsonEncoder.outputFormatting = .prettyPrinted
        jsonEncoder.dateEncodingStrategy = .secondsSince1970
        
        dateFormatter.dateFormat = "yyyyMMdd_HHmmss"
        dateFormatter.timeZone = TimeZone(abbreviation: "UTC")
    }
    
    /// Finds the offset between the phone compass and the true COG.
    class CompassOffsetFilter {
        var offset: Double = 0.0
        var updateCount: Int = 0
        
        var speedThreshold: Double = 0.0
        var errorClip: Double = 0.0
        var gain: Double = 0.0
        
        init(speedThreshold: Double, errorClip: Double, gain: Double, offset: Double = 90.0) {
            // The default offset generally 90 degrees
            self.speedThreshold = speedThreshold
            self.errorClip = errorClip
            self.gain = gain
            self.offset = offset
        }
        
        func update(sog: Double, cog: Double, hdg: Double) {
            // Only use times when speed is sufficient, for small SOG the COG is undefined
            if sog > speedThreshold {
                updateCount += 1
                // The residual error
                var error = (hdg + offset) - cog
                // Deal with the angle wrap around
                if error > 180 {
                    error -= 360
                } else if error < -180 {
                    error += 360
                }
                // Clip the error if it is too large.  Adds robustness
                let clippedError = max(min(error, errorClip), -errorClip)
                // Update the offset with a gradually reduced rate
                let reducedGain = gain * (0.0000 + 10.0 / (Double(updateCount) + 100.0))
                offset -= reducedGain * clippedError
            }
        }
    }

    /// Log the current state.
    func logState() {
        stateLog.append(state)
    }

    /// This is the offset to the bow (in NED) from the current sensor position.
    func bowOffset() -> Vector {
        return Globals.distanceToBow * Vector(cos(Mapping.deg2rad(state.hdg)),
                                              sin(Mapping.deg2rad(state.hdg)),
                                              0.0)
    }
    
    func updateComputedState() {

        // If boat LLA is initialized.
        if state.boatLLA != Coord() {
            let rawNed = Mapping.LLA2NED(lla: state.LLA, lla0: state.boatLLA)
            state.ned = rawNed + bowOffset()
        }
        
        // Compass calculations.  First move to TRUE/MAG as set in config
        let cog = Mapping.compassAngle(Globals.compassAdjustTrue(trueNorthDegrees: state.cog))
        // Update the offset filter.
        compassOffsetFilter.update(sog: state.sog, cog: cog, hdg: state.rawHdg)
        state.hdg = Mapping.compassAngle(compassOffsetFilter.offset + state.rawHdg)

        // Tack calculations.

        if stateLog.count > tackDelay {
            tackDetector.update(hdg: state.hdg, old_hdg: stateLog[stateLog.count-tackDelay].hdg)
            state.tackState = tackDetector.state
            state.afterTackHdg = tackDetector.hdg_post_tack
        }
        
        if let sline = startingLine {
            
            let nedVelocity = state.sog * Vector(cos(Mapping.deg2rad(state.cog)),
                                                 sin(Mapping.deg2rad(state.cog)),
                                                 0.0)
            // This is subtle.  If the distance is positive, and we are closing to the line, then the
            // speed should be negative.  But that seems unatural, so flip the sign.
            let obsSpeedToLine = -1.0 * sline.vPerp.dot(nedVelocity)
            state.speedToLine = speedFilter.update(obsSpeedToLine)
            
            // Line calculations
            state.distanceToLine = distanceToLine()
            state.timeToLine = timeToLine()
        }

    }

    func trackData() -> Data? {
        do {
            // print"Encoding \(track.count) track points")
            return try jsonEncoder.encode(stateLog)
        } catch {
            // print"Error Could not json encode")
            return nil
        }
    }

    func pingPin() {
        state.pinLLA = state.LLA
        pinOffset = bowOffset()
        setLine()
        
        // Log that the pin was marked
        state.isPin = true
        logState()
        state.isPin = false
    }
    
    func pingPinEdge() {
        if startingLine == nil { return }
        state.pinNED = Mapping.projectPoint(line: startingLine!, point: state.ned)
        startingLine = Mapping.makeLine(from: state.boatNED, to: state.pinNED)
    }

    func pingBoatEdge() {
        if startingLine == nil { return }
        state.boatNED = Mapping.projectPoint(line: startingLine!, point: state.ned)
        startingLine = Mapping.makeLine(from: state.boatNED, to: state.pinNED)
    }

    func pingBoat() {
        state.boatLLA = state.LLA
        boatOffset = bowOffset()
        setLine()

        // Log that the boat was marked
        state.isBoat = true
        logState()
        state.isBoat = false
    }
    
    func setLine() {
        if state.boatLLA != Coord() {
            state.boatNED = Mapping.LLA2NED(lla: state.boatLLA, lla0: state.boatLLA) + boatOffset
            if state.pinLLA != Coord() {
                state.pinNED = Mapping.LLA2NED(lla: state.pinLLA, lla0: state.boatLLA) + pinOffset
                if state.boatNED != state.pinNED {
                    startingLine = Mapping.makeLine(from: state.boatNED, to: state.pinNED)
                }
            }
        }
    }
    
    func linePerpAngle() -> Double {
        guard let sl = startingLine else { return 0.0 }
        let angle = Mapping.compassAngleOfLine(line: sl) // heading of BOAT -> PIN
        return Globals.compassAdjustTrue(trueNorthDegrees: angle + 90)  // This is the angle that points "upwind"
    }
    
    func distanceToLine() -> Double {
        guard let sl = startingLine else { return 0.0 }
        return Mapping.distance2Line(line: sl, point: state.ned)
    }
    
    func timeToLine() -> Double {
        if startingLine == nil { return 0.0 }
        if (state.speedToLine > 0.3) && (state.distanceToLine > 1.0){
            return state.distanceToLine / state.speedToLine
        }
        return 0.0
    }
    
    /// Return a percent with 1.0 being the pin, and 0.0 being the boat
    func distanceAlongLine() -> Double {
        guard let sl = startingLine else { return 0.0 }
        return Mapping.distanceAlongLine(line: sl, point: state.ned)
    }
    
    /// Return the length of the line
    func lineLength() -> Double {
        guard startingLine != nil else { return 10.0 }
        return (state.pinNED - state.boatNED).length
    }

    func saveFilename() -> String {
        return "boatstate_" + dateFormatter.string(from: stateLog[0].dateTime)
    }
    
    func saveTrackSynchronous() {
        if stateLog.count > 0 {
            print("Save Track Sync!")
            guard let trackData = self.trackData() else {
                print("Error")
                return
            }
            Uploader.uploadToAWS(filename: saveFilename(), data: trackData)
        } else {
            print("Zero entries in log to save!!!")
        }
    }
    
    func saveTrack() {
        DispatchQueue.global().async {
            print("Save Track Async!")
            self.saveTrackSynchronous()
        }
    }
}

