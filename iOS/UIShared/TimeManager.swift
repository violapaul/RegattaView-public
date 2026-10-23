//
//  TimeManager.swift
//  BoatTimer
//
//  Created by Paul Viola on 12/2/22.
//

import Foundation
import AVFoundation
import SwiftUI

class Signals {
    var signalList: [Int : () -> Void] = [:]
    
    init() {
        reset()
    }
    
    func reset() {
        for minute in 1..<10 {
            signalList[minute * 60] = Alert.warn1
        }
        for second in [50, 40] {
            signalList[second] = Alert.warn2
        }
        
        signalList[30] = Alert.warn30
        signalList[20] = Alert.warn20
        signalList[10] = Alert.warn10

        for second in 5..<10 {
            signalList[second] = Alert.warn10
        }

        for second in 1..<5 {
            signalList[second] = Alert.warn1
        }

        signalList[0] = Alert.start
    }
    
    func trigger(_ step: Int) {
        guard let handler = signalList[step] else {
            return
        }
        print("Trigger on \(step)")
        handler()
        signalList.removeValue(forKey: step)
    }
}

extension TimeInterval {
    var hourMinuteSecondMS: String {
        String(format:"%d:%02d:%02d.%03d", hour, minute, second, millisecond)
    }
    var minuteSecondMS: String {
        String(format:"%d:%02d.%03d", minute, second, millisecond)
    }
    var hour: Int {
        Int((self/3600).truncatingRemainder(dividingBy: 3600))
    }
    var minute: Int {
        Int((self/60).truncatingRemainder(dividingBy: 60))
    }
    var second: Int {
        Int(truncatingRemainder(dividingBy: 60))
    }
    var millisecond: Int {
        Int((self*1000).truncatingRemainder(dividingBy: 1000))
    }
}

class TimeManager: NSObject, ObservableObject {
    
    @Published var timeString: String = "00:00"
    @Published var timeToStart = 0.0
    @Published var secondsToStart = 0

    var sequenceTime = 5 * 60.0
    var startTime = Date.now
    var timer: Timer?
    var formatter: DateComponentsFormatter?
    var signals = Signals()

    init(sequenceTime: Double = 5 * 60.0) {
        self.sequenceTime = sequenceTime
        super.init()
        timeString = timeFormat(delta: sequenceTime)
    }
    
    func timeFormat(delta: TimeInterval) -> String {
        if formatter == nil {
            formatter = DateComponentsFormatter()
            formatter?.allowedUnits = [.minute, .second]
            formatter?.zeroFormattingBehavior = .pad
        }
        return formatter?.string(from: delta + 0.95) ?? "--:--"
    }

    /// Triggers the starting sequence countdown.
    func gun() {
        signals.reset()
        startTime = Date.now + TimeInterval(sequenceTime)

        if self.timer == nil {
            self.timer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true, block: { _ in
                self.showTime()
            })
        }
        Alert.warn10()
    }

    /// Creates the time we want to show.
    func showTime() {
        if timer == nil {
            /// If we are not counting down then we are waiting to start.
            timeString = timeFormat(delta: sequenceTime)
        } else {
            var delta = startTime.timeIntervalSince(Date.now)
            signals.trigger(Int(ceil(delta)))
            timeToStart = delta
            secondsToStart = Int(ceil(delta))
            delta = delta < 0 ? -delta : delta
            // Adding an offset so that
            timeString = timeFormat(delta: delta)
        }
    }
    
    /// Stop counting down and reset the start time.
    func reset() {
        signals.reset()
        timer?.invalidate()
        timer = nil
        startTime = Date.now + TimeInterval(sequenceTime)
        showTime()
    }

    /// Update the time until the start.
    func timeDelta(seconds: Double) {
        signals.reset()
        if timer == nil {
            sequenceTime = sequenceTime + seconds
        } else {
            startTime = startTime + TimeInterval(seconds)
        }
        showTime()
    }

    func sync() {
        signals.reset()
        let delta = self.startTime.timeIntervalSince(Date.now)
        let deltaMins = round(delta / 60.0)
        startTime = Date.now + TimeInterval(deltaMins * 60.0)
        showTime()
    }
    
}
