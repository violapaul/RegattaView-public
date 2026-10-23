
//
//  Created by Paul Viola on 11/21/22.
//

import Foundation
import CoreLocation
import SwiftUI
import Spatial

/// Collects and maintains information from the boat sensors.  Also provides information about the current state.
/// Currently supports the iphone sensors (GPS/Compass) and the RaceBox sensors (GPS).
///
/// In the future will likely support IMU and potentially other boat data (over WIFI).
///
class BoatManager: BoatState {
    
    // These are the types of sensors we can interact with.
    var locationManager = LocationManager()
    var raceBoxManager = RaceBoxManager()
    
    var phoneLocation: CLLocation?
    var raceBoxResponse: RaceBoxManager.Response?
    

    // MARK: - controls message frequency
    static let logInterval = 0.04
    static let updateInterval = 0.04
    // the throttler ensures we do not write too many messages to the log.
    // var throttler = Throttler(deltaT: messageInterval)
    var logTimer: Timer?    // Used to log messages.
    var updateTimer: Timer? // Used to update computed states
    
    override init() {
        UIApplication.shared.isIdleTimerDisabled = true
        UIDevice.current.isBatteryMonitoringEnabled = true
        super.init()
        
        // Event handling is multifaceted.
        locationManager.addOnChanged(onLocationChanged)
        raceBoxManager.addOnChanged(onRaceBoxChanged)
        
        updateTimer = Timer.scheduledTimer(withTimeInterval: BoatManager.updateInterval, repeats: true, block: { _ in
            self.updateComputedState()
        })
    }
    
    func onLocationChanged() {
        state.event = .phone
        updateCurrentState()
    }

    func onRaceBoxChanged() {
        logState()
        state.event = .raceBox
        updateCurrentState()
    }
    
    // Racebox functions
    func hasRaceBox() -> Bool {
        return raceBoxManager.hasRaceBox()
    }
    
    func raceBoxConnect() {
        return raceBoxManager.connect()
    }

    func status() -> String {
        
        var res = String(format: "%05d, ", stateLog.count)
        res += raceBoxManager.status
        return res
    }


    /// Update the state of the boat
    func updateCurrentState() {
        state.dateTime = Date.now
        state.batteryLevel = Double(UIDevice.current.batteryLevel)
        
        if let loc = locationManager.lastLocation {
            state.phoneGPS = GPSPt(lat: loc.coordinate.latitude, lon: loc.coordinate.longitude,
                                   sog: loc.speed, cog: loc.course,
                                   accuracy: loc.horizontalAccuracy, isValid: true)
        }
        
        let rbLoc = raceBoxManager.currentResponse
        state.raceBoxGPS = GPSPt(lat: rbLoc.lat, lon: rbLoc.lon, sog: rbLoc.sog, cog: rbLoc.cog, accuracy: rbLoc.hAccuracy, isValid: true)

        // Use the racebox if it is available and the response is good.
        if raceBoxManager.responseIsGood {
            state.LLA = Vector(rbLoc.lat, rbLoc.lon, 0.0)
            state.sog = rbLoc.sog
            state.cog = rbLoc.cog
            state.accuracy = rbLoc.hAccuracy
            state.force = Vector(rbLoc.forceX, rbLoc.forceY, rbLoc.forceZ)
        } else {
            if let location = locationManager.lastLocation {
                state.LLA = Vector(location.coordinate.latitude, location.coordinate.longitude, 0.0)
                state.sog = location.speed
                state.cog = location.course
                state.accuracy = location.horizontalAccuracy
            }
        }
        
        if let lmHeading = locationManager.lastHeading {
            // Note, the boat is mostly (?!?!?) magnetic heading.  This will convert to the prefered.
            state.rawHdg = Globals.compassAdjustMagnetic(magneticNorthDegrees: lmHeading.magneticHeading)
        }

        // Update the UX.
        objectWillChange.send()
    }
    
    
    /// Log the current state.
    override func logState() {
        stateLog.append(state)
        // Save the track every 30 minutes... assuming 25hz
        if stateLog.count % (30 * 60 * 25) == 0 {
            saveTrack()
        }
    }

}
