//
//  Signals.swift
//  BoatStart
//
//  Created by Paul Viola on 1/28/23.
//

import Foundation



/// Used to filter streams of true/false looking for runs of true that are at least min_true long
/// with no dropouts of false longer than max_drop_out
class RunCapture {
    
    // instance properties
    var trueTime: Int
    var falseTime: Int
    var minTrue: Int
    var maxDropOut: Int
    var triggered: Bool
    
    // constructor
    init(minTrue: Int, maxDropOut: Int) {
        self.trueTime = 0
        self.falseTime = 0
        self.minTrue = minTrue
        self.maxDropOut = maxDropOut
        self.triggered = false
    }
    
    // instance methods
    func addTrue() {
        trueTime += 1
        falseTime = 0
    }
    
    /// Return true if a run in done.
    func addFalse() {
        if trueTime > 0 {
            print(trueTime, falseTime)
        }
        falseTime += 1
        if falseTime > maxDropOut { // too many negs, we are done
            if trueTime > minTrue { // were there enough positives?
                triggered = true
            }
            trueTime = 0
            falseTime = 0
        }
    }
    
    /// Return True only once!
    func isTriggered() -> Bool {
        if triggered {
            triggered = false
            reset()
            return true
        } else {
            return false
        }
    }
    
    func reset() {
        trueTime = 0
        falseTime = 0
    }
}

/// Used to delay the trigger of an event by a count of events.
class DelayTimer {
    
    // instance properties
    var delay: Int
    var count: Int
    var triggered: Bool
    
    // constructor
    init(delay: Int) {
        self.delay = delay
        self.count = 0
        self.triggered = false
    }
    
    func start() {
        count = delay
        triggered = false
    }
    
    /// Call once per event to count
    func update() {
        if count > 0 {
            count -= 1
            if count == 0 {
                triggered = true
            }
        }
    }
    
    func isWaiting() -> Bool {
        return count > 0
    }
    
    /// Return True only once!
    func isTriggered() -> Bool {
        if triggered {
            triggered = false
            reset()
            return true
        } else {
            return false
        }
    }
    
    func reset() {
        count = 0
    }
    
    func getCount() -> Int {
        return count
    }
}

/// "Consumes values and returns the running average"
class RunningAverage {
    
    // instance properties
    var sum: Double
    var count: Double
    
    init() {
        self.sum = 0
        self.count = 0
    }
    
    func add(value: Double) {
        sum += value
        count += 1
    }
    
    func average() -> Double {
        return sum / count
    }
    
    func reset() {
        sum = 0
        count = 0
    }
}

func angle_difference(a: Double, b: Double) -> Double {
    // Return the difference between two angles in degrees
    return abs(a - b + 180).truncatingRemainder(dividingBy: 360) - 180
}

class IncrementalTackDetector {
    
    enum TackHdgState: String, Codable {
        case invalid
        case inTack
        case afterTack
        case averagingHdg
        case valid
    }
    
    var tack_capture: RunCapture
    var after_tack_delay: DelayTimer
    var hdg_capture_delay: DelayTimer
    var hdg_average: RunningAverage
    var hdg_post_tack: Double
    var state: TackHdgState
    
    init(tack_time: Int, after_tack_time: Int, hdg_average_time: Int) {
        tack_capture = RunCapture(minTrue: tack_time, maxDropOut: 5)
        after_tack_delay = DelayTimer(delay: after_tack_time)
        hdg_capture_delay = DelayTimer(delay: hdg_average_time)
        hdg_average = RunningAverage()
        hdg_post_tack = 0.0
        state = .invalid
    }
    
    func update(hdg: Double, old_hdg: Double) {
        // Detect and capture tacks
        let delta = abs(angle_difference(a: hdg, b: old_hdg))
        if delta > 80 && delta < 135 {
            state = .inTack
            tack_capture.addTrue()
        } else {
            tack_capture.addFalse()
        }
        
        if tack_capture.isTriggered() {
            state = .afterTack
            after_tack_delay.start()
        }
        
        // Wait to the capture of the heading after the tack
        if after_tack_delay.isWaiting() {
            after_tack_delay.update()
        }
        
        if after_tack_delay.isTriggered() {
            state = .averagingHdg
            hdg_capture_delay.start()
            hdg_average.reset()
        }
        
        // Capture the heading, by averaging
        if hdg_capture_delay.isWaiting() {
            hdg_capture_delay.update()
            hdg_average.add(value: hdg)
            hdg_post_tack = hdg_average.average()
        }
        if hdg_capture_delay.isTriggered() {
            state = .valid
        }
    }
    
    func postTackHeading() -> Double? {
        if state == .valid {
            return hdg_post_tack
        } else {
            return nil
        }
    }
}
