//
//  SequenceStats.swift
//  BoatStart
//
//  Created by Paul Viola on 3/28/24.
//

import Foundation

/// Class to analyze a sequence of events.  By default the assumption is a sequence of times, but it can be anything.  Manages and returrns
/// the min, max, and exponential average of the last N events (default 50).  The average is weighted by the alpha value, which is the weight of the current event.
class SequenceStats {
    var alpha: Double
    var value: Double = 1
    var lastTime: Date
    var history: [Double] // store the last 50 times
    var count: Int
    
    init(alpha: Double, count: Int = 50) {
        self.alpha = alpha
        self.lastTime = Date()
        self.history = [0] // Just to make sure there is one
        self.count = count
    }
    
    func reset() {
        lastTime = Date()
        value = 1
        history = [0]
    }
    
    func event() {
        let now = Date()
        let delta = now.timeIntervalSince(lastTime)
        lastTime = now
        internalUpdate(value: delta)
    }
    
    private func internalUpdate(value: Double) {
        self.value = alpha * value + (1 - alpha) * self.value
        history.append(value)
        if history.count > count {
            history.removeFirst()
        }
    }
    
    func update(value: Double) {
        internalUpdate(value: value)
    }
    
    func get() -> (Double, Double, Double) {
        // return the running average and the min and max of the last 50 times
        return (value, history.min() ?? 0, history.max() ?? 0)
    }
    
    func avg() -> Double {
        let val = history.reduce(0, +) / Double(history.count)
        return val
    }
    
    func min() -> Double {
        let now = Date()
        let delta = now.timeIntervalSince(lastTime)
        let val = history.min() ?? 0
        return Swift.min(delta, val)
    }
    
    func max() -> Double {
        let now = Date()
        let delta = now.timeIntervalSince(lastTime)
        let val = history.max() ?? 0
        return Swift.max(delta, val)
    }
    
    func format(floatFormat: String = "{:7.3f}") -> String {
        let (avg, minTime, maxTime) = get()
        // computed formatted versions
        let formattedAvg = String(format: floatFormat, avg)
        let formattedMin = String(format: floatFormat, minTime)
        let formattedMax = String(format: floatFormat, maxTime)
        return "avg = \(formattedAvg), min = \(formattedMin), max = \(formattedMax)"
    }
}
